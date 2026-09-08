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

    An enum, not a Literal: this is a discriminator the runner branches
    on, and a Literal is only checked where it is annotated -- the
    runner's `dataset.scope == "region"` comparisons were bare strings
    that no type would have caught if one were misspelled.

    VARIANT is the third value: the screen loop also runs OUTSIDE the
    dataset, and the same reasoning as the region loop applies word for
    word -- `sync_run_items` granularity naturally becomes
    (dataset x screen x table), and one screen failing doesn't mark a
    neighboring screen `failed`.
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

        `for_region` used to enumerate fields BY HAND, so when `variant` was
        added without also being listed there it SILENTLY dropped on the
        region branch. Collecting that into one method did not remove the
        trap, it only moved it: a hand-written constructor call here sets it
        again for the next field anyone adds.

        `dataclasses.replace` removes it for real. It copies every init
        field, so a new one is carried without this method being touched,
        and it raises on a name that is not a field instead of ignoring the
        change. `_cache` is an init field, so the copy is handed the same
        dict object and the cache stays shared.
        `DomainContext._clone` (domain/base.py) does the same.
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
    # Declared here rather than read off whatever the subclass happens to
    # have. Market datasets DO expose resources -- the screener alone
    # declares four -- so the catalogue builder names the field instead of
    # reaching for it with `getattr`. Note that the declaration does not
    # catch a misspelling: this default means `apis = (...)` still reads
    # back as `()` (see `Registrable`).
    api: tuple[ApiExposure, ...] = ()
    scope: MarketScope = MarketScope.GLOBAL

    def variants(self, settings: Settings, state: VariantState | None) -> Sequence[str]:
        """For `scope == "variant"`, the outer loop's keys.

        `state` is the storage side of the question: the variant set also
        depends on the `is_enabled` column of the `screens` table, which
        `Settings` cannot answer. It is a one-method protocol rather than
        a Session so that nothing in this package depends on the ORM.

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

    def upsert(
        self, writer: RowWriter, result: NormalizedResult, *, full_refresh: bool = False
    ) -> WriteStats:
        """`full_refresh` is accepted and ignored: an ungated market dataset
        writes everything it normalized either way."""
        return plain_upsert(writer, result)


class SnapshotGlobalDataset[RawT](SnapshotWrite, GlobalDataset[RawT]):
    """Market dataset that writes a snapshot + history.

    The policy itself is `SnapshotWrite`, the same object the symbol side
    uses: "compare against the snapshot table, write both it and the
    history" is one rule, and it was written out twice here until the two
    copies disagreed about whether `key_columns` had a default.
    """
