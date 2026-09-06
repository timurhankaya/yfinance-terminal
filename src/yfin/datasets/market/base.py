"""Market-scoped dataset contract.

`MarketContext` does NOT INHERIT from `SyncContext`: a shared base class
would suggest the `symbol` field exists on the market side too. The only
difference is that `normalize` takes no `symbol`; TableWrite,
NormalizedResult, WriteStats, RowWriter, and snapshot logic are shared.

The region loop lives OUTSIDE the dataset (in market_runner): this makes
sync_run_items granularity naturally (dataset x table x region), so
WriteStats doesn't need to carry a region breakdown.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from sqlalchemy.orm import Session

from yfin.core.config import Settings
from yfin.datasets.base import NormalizedResult
from yfin.datasets.snapshot_base import snapshot_upsert
from yfin.storage.contracts import RowWriter, WriteStats, apply_write

# A third value: the screen loop also runs OUTSIDE the dataset -- the same
# reasoning as the region loop above applies word for word:
# `sync_run_items` granularity naturally becomes (dataset x screen x table),
# and one screen failing doesn't mark a neighboring screen `failed`.
MarketScope = Literal["global", "region", "variant"]


@dataclass
class MarketContext:
    """Symbol-less context for a run."""

    fetched_at: datetime
    start: date
    end: date
    region: str | None = None
    # For `scope="variant"`, the outer loop's key for that kind (screen
    # name). SEPARATE from `region` because a screen is not a region, and
    # `sync_run_items.region` carries region semantics.
    variant: str | None = None
    _cache: dict[str, Any] = field(default_factory=dict, repr=False)

    def cached(self, key: str, fn: Callable[[], Any]) -> Any:
        """The Calendars instance is shared across four calendar datasets;
        the Market instance is shared once per region."""
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def _clone(self, **changes: Any) -> MarketContext:
        """A copy that SHARES the cache -- the single cloning point.

        `for_region` used to enumerate fields BY HAND. When `variant` was
        added without also listing it there, it would SILENTLY drop on the
        region branch; every future field would set the same trap.
        `DomainContext._clone` (domain/base.py) solves the same problem the
        same way.
        """
        clone = MarketContext(
            fetched_at=changes.get("fetched_at", self.fetched_at),
            start=changes.get("start", self.start),
            end=changes.get("end", self.end),
            region=changes.get("region", self.region),
            variant=changes.get("variant", self.variant),
        )
        clone._cache = self._cache
        return clone

    def for_region(self, region: str) -> MarketContext:
        """A context with region set, sharing the same cache."""
        return self._clone(region=region)

    def for_variant(self, variant: str) -> MarketContext:
        """A context with variant set, sharing the same cache.

        Does NOT write to the `region` field: a screen is not a region.
        """
        return self._clone(variant=variant)


class GlobalDataset[RawT](ABC):
    name: str
    depends_on: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    scope: MarketScope = "global"

    def variants(self, settings: Settings, session: Session | None) -> Sequence[str]:
        """For `scope == "variant"`, the outer loop's keys.

        `session` is REQUIRED, and is the most debatable part of this
        signature: the variant set looks at the `is_enabled` column on the
        `screens` table. A signature taking only `settings` could not read
        the DB -- `Settings` is a Pydantic settings object.

        `market_regions()` is a MODULE FUNCTION in the runner because the
        region set comes only from config and is THE SAME for every
        region-scoped dataset. The variant set is dataset-specific and
        reads the DB; a declarative extension point on the base class beats
        the runner knowing about the `screens` table.

        Defaulting to empty is MANDATORY: the six datasets with
        `scope != "variant"` never override this, and the runner never
        routes them through this branch.
        """
        return ()

    @abstractmethod
    def fetch(self, mctx: MarketContext) -> RawT: ...

    @abstractmethod
    def normalize(self, raw: RawT) -> NormalizedResult: ...

    def upsert(self, writer: RowWriter, result: NormalizedResult) -> WriteStats:
        stats = WriteStats(skipped=dict(result.skipped))
        for write in result.writes:
            apply_write(writer, write, stats)
        return stats


class SnapshotGlobalDataset[RawT](GlobalDataset[RawT]):
    """Market dataset that writes a snapshot + history.

    Shares the SAME policy as `SnapshotDataset` on the symbol side:
    comparison against the snapshot table, write to _history; key columns
    are declarative (("region",) for market_status, ("region",
    "board_code") for market_summary).
    """

    snapshot_table: str
    history_table: str
    key_columns: tuple[str, ...]

    def upsert(self, writer: RowWriter, result: NormalizedResult) -> WriteStats:
        return snapshot_upsert(
            writer,
            result,
            snapshot_table=self.snapshot_table,
            history_table=self.history_table,
            key_columns=self.key_columns,
        )
