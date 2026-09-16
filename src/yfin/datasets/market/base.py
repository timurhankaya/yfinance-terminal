"""Market-scoped dataset contract.

`MarketContext` does not inherit from `SyncContext`: no `symbol` here. The
region loop lives in market_runner, so items are (dataset x table x region).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from yfin.core.config import Settings
from yfin.datasets.base import NormalizedResult, plain_upsert
from yfin.datasets.exposure import ApiExposure
from yfin.datasets.snapshot_base import SnapshotWrite
from yfin.storage.contracts import RowWriter, VariantState, WriteStats


class MarketScope(StrEnum):
    """Which outer loop a market dataset runs inside.

    An enum, not a Literal, so the runner's branch comparisons are typed.
    VARIANT runs the screen loop outside the dataset, one item per screen.
    """

    GLOBAL = "global"
    REGION = "region"
    VARIANT = "variant"


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

        `dataclasses.replace` carries every init field, so a field added
        later is never silently dropped.
        """
        return replace(self, **changes)

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
    # Declared so the catalogue builder can name the field; a misspelled
    # override still reads back as `()` (see `Registrable`).
    api: tuple[ApiExposure, ...] = ()
    scope: MarketScope = MarketScope.GLOBAL

    def variants(self, settings: Settings, state: VariantState | None) -> Sequence[str]:
        """For `scope == "variant"`, the outer loop's keys.

        `state` is a one-method protocol, not a Session, so this package
        stays free of the ORM. Non-variant datasets keep the empty default.
        """
        return ()

    @abstractmethod
    def fetch(self, mctx: MarketContext) -> RawT: ...

    @abstractmethod
    def normalize(self, raw: RawT) -> NormalizedResult: ...

    def upsert(
        self, writer: RowWriter, result: NormalizedResult, *, full_refresh: bool = False
    ) -> WriteStats:
        """`full_refresh` is accepted and ignored: an ungated market dataset
        writes everything it normalized either way."""
        return plain_upsert(writer, result)


class SnapshotGlobalDataset[RawT](SnapshotWrite, GlobalDataset[RawT]):
    """Market dataset that writes a snapshot + history via the shared `SnapshotWrite`."""
