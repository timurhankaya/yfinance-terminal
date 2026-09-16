"""Contract for domain (sector / industry) scoped datasets.

Not derived from `SyncContext`: domain tables' `symbol` means the company, not
the target. The region loop is in domain_runner: items are (dataset x key x region).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any

from yfin.datasets.asof_base import (
    DOMAIN_GATE_KEY_COLUMNS,
    DOMAIN_GATE_TABLE,
    GLOBAL_REGION_MARKER,
    AsOfGate,
)
from yfin.datasets.base import NormalizedResult, plain_upsert
from yfin.datasets.exposure import ApiExposure
from yfin.models.domains import DomainType
from yfin.storage.contracts import RowWriter, WriteStats

#: `DomainType` is the SAME enum the `domains` table is typed with
#: (`models.domains`); the runner uses it for queries and target lookup.


@dataclass
class DomainContext:
    """Per-run context, symbol-less and key-targeted.

    `for_target` and `for_region` share `_cache`, so a (key, region) pair's
    raw JSON is fetched once for all of that pair's datasets.
    """

    fetched_at: datetime
    as_of_date: date
    primary_region: str
    region: str = GLOBAL_REGION_MARKER
    key: str | None = None
    domain_type: DomainType | None = None
    # Industry key -> parent sector key in the DB. The runner populates
    # this from the DB after the `domain_taxonomy` pass; `industry_profile`
    # compares it against the response's `sectorKey` and warns on a
    # mismatch (a signal of taxonomy drift).
    parents: dict[str, str] = field(default_factory=dict, repr=False)
    _cache: dict[str, Any] = field(default_factory=dict, repr=False)

    def cached(self, key: str, fn: Callable[[], Any]) -> Any:
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def _clone(self, **changes: Any) -> DomainContext:
        """A copy that SHARES the cache and the parent map by identity."""
        return replace(self, **changes)

    def for_target(self, key: str, domain_type: DomainType) -> DomainContext:
        return self._clone(key=key, domain_type=domain_type)

    def for_region(self, region: str) -> DomainContext:
        return self._clone(region=region)

    # --- values derived for fetching -----------------------------------

    @property
    def fetch_region(self) -> str:
        """The region actually requested; a region-less pass uses the primary region's cache."""
        return self.primary_region if self.region == GLOBAL_REGION_MARKER else self.region

    @property
    def target_key(self) -> str:
        if self.key is None:  # pragma: no cover - defensive
            raise ValueError("fetch was called before DomainContext.key was set")
        return self.key

    @property
    def target_type(self) -> DomainType:
        if self.domain_type is None:  # pragma: no cover - defensive
            raise ValueError("fetch was called before DomainContext.domain_type was set")
        return self.domain_type


class DomainDataset[RawT](ABC):
    """Fetching, normalizing, and writing a single (key, region) pair."""

    name: str
    depends_on: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()  # table names it writes
    # Declared, not read reflectively -- see the note on GlobalDataset.
    api: tuple[ApiExposure, ...] = ()
    # Which key set it iterates over
    scope: DomainType = DomainType.SECTOR
    # True enters the region loop; False runs a single pass (`region='*'`)
    regional: bool = False
    # False means the dataset does not enter the key loop and runs once.
    # Only bootstrap (`domain_taxonomy`) is like this: the whole taxonomy is
    # written in one transaction -- it is either consistent as a whole or
    # not written at all.
    per_key: bool = True

    @abstractmethod
    def fetch(self, ctx: DomainContext) -> RawT: ...

    @abstractmethod
    def normalize(self, raw: RawT, key: str) -> NormalizedResult: ...

    def upsert(
        self, writer: RowWriter, result: NormalizedResult, *, full_refresh: bool = False
    ) -> WriteStats:
        """`full_refresh` is accepted and ignored: an ungated domain dataset
        writes everything it normalized either way."""
        return plain_upsert(writer, result)


class DomainAsOfDataset[RawT](AsOfGate, DomainDataset[RawT]):
    """as-of gated domain dataset; shares the gate mixin, not the `Dataset` hierarchy."""

    asof_gate_table = DOMAIN_GATE_TABLE
    asof_gate_key_columns = DOMAIN_GATE_KEY_COLUMNS

    def gate_identity(self, result: NormalizedResult) -> dict[str, Any]:
        """Gate key: (domain_key, dataset, region).

        `region` is read back from the rows because dataset instances are
        singletons reused across the region loop.
        """
        first = self.gate_row(result)
        return {
            "domain_key": first["domain_key"],
            "dataset": self.name,
            "region": first.get("region", GLOBAL_REGION_MARKER),
        }
