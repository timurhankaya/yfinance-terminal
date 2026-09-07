"""Contract for domain (sector / industry) scoped datasets.

Sibling of `GlobalDataset`. Cannot derive from `SyncContext`: the `symbol`
field would carry the wrong meaning on the domain side -- sharper here than
the same issue in `market/base.py`, because domain tables really do have a
`symbol` column, but it is the company's symbol.

A third axis: neither a symbol loop nor a region loop. 156 keys, each its
own HTTP request. The region loop lives outside the dataset (inside
domain_runner), so `sync_run_items` granularity naturally becomes
(dataset x key x region x table).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from yfin.datasets.asof_base import (
    DOMAIN_GATE_KEY_COLUMNS,
    DOMAIN_GATE_TABLE,
    GLOBAL_REGION_MARKER,
    AsOfGate,
    first_row,
)
from yfin.datasets.base import NormalizedResult
from yfin.storage.contracts import RowWriter, WriteStats, apply_write

DomainType = Literal["sector", "industry"]


@dataclass
class DomainContext:
    """Per-run context, symbol-less and key-targeted.

    `for_target` and `for_region` share the same `_cache` (mirrors
    `MarketContext.for_region`): a (key, region) pair's raw JSON is fetched
    once and feeds all of that pair's datasets. Region-less datasets use
    the primary region's response and produce no extra request (data was
    measured to be region-independent).
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
        clone = DomainContext(
            fetched_at=self.fetched_at,
            as_of_date=self.as_of_date,
            primary_region=self.primary_region,
            region=changes.get("region", self.region),
            key=changes.get("key", self.key),
            domain_type=changes.get("domain_type", self.domain_type),
            parents=self.parents,
        )
        clone._cache = self._cache
        return clone

    def for_target(self, key: str, domain_type: DomainType) -> DomainContext:
        return self._clone(key=key, domain_type=domain_type)

    def for_region(self, region: str) -> DomainContext:
        return self._clone(region=region)

    # --- values derived for fetching -----------------------------------

    @property
    def fetch_region(self) -> str:
        """The region actually requested.

        In a region-less pass, `region` is `'*'`; that request goes to the
        primary region and shares its cache with regional passes -- no
        second HTTP request is made if the primary region is already fetched.
        """
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
    # Which key set it iterates over
    scope: DomainType = "sector"
    # True enters the region loop; False runs a single pass (`region='*'`)
    regional: bool = False
    # False means the dataset does not enter the key loop and runs once.
    # Only bootstrap (`domain_taxonomy`) is like this: 156 `symbols` rows
    # and 156 `domains` rows are written in one transaction -- the taxonomy
    # is either consistent as a whole or not written at all.
    per_key: bool = True

    @abstractmethod
    def fetch(self, ctx: DomainContext) -> RawT: ...

    @abstractmethod
    def normalize(self, raw: RawT, key: str) -> NormalizedResult: ...

    def upsert(self, writer: RowWriter, result: NormalizedResult) -> WriteStats:
        stats = WriteStats(skipped=dict(result.skipped))
        for write in result.writes:
            apply_write(writer, write, stats)
        return stats


class DomainAsOfDataset[RawT](AsOfGate, DomainDataset[RawT]):
    """as-of gated domain dataset.

    Cannot derive from `AsOfDataset`: that class sits under `Dataset[RawT]`
    and carries the `fetch(SyncContext)` / `normalize(raw, symbol)`
    signature. What is shared is the gate logic, not the hierarchy -- which
    is why `AsOfGate` is a mixin.
    """

    asof_gate_table = DOMAIN_GATE_TABLE
    asof_gate_key_columns = DOMAIN_GATE_KEY_COLUMNS

    def gate_identity(self, result: NormalizedResult) -> dict[str, Any]:
        """Gate key: (domain_key, dataset, region).

        `region` is not kept as state on the dataset instance (registry
        datasets are singletons reused across the region loop); the value
        passed from `DomainContext` to `normalize()` per pass is written
        into rows and read back from there. Region-less datasets have no
        `region` column in their rows -> defaults to `'*'`.
        """
        first = first_row(result)
        return {
            "domain_key": first["domain_key"],
            "dataset": self.name,
            "region": first.get("region", GLOBAL_REGION_MARKER),
        }
