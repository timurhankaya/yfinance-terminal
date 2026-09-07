"""Dataset contract.

Independent of SQLAlchemy and the database: defines which data goes to
which table, with which keys, and with what column scope. How the write
happens lives in `yfin.storage.persistence`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal, Protocol

from yfin.datasets.exposure import ApiExposure
from yfin.storage.contracts import (
    RowWriter,
    TableWrite,
    WriteStats,
    apply_write,
)


class WatermarkProvider(Protocol):
    """Contract for a read-only watermark provider.

    `Callable[..., date | datetime | None]` is not enough: `...` turns off
    argument checking entirely, so a wrong call site would not be caught by
    typing. The Protocol also documents that `where` is keyword-only and
    optional -- existing call sites (history, shares_full) omit it.
    """

    def __call__(
        self,
        table: str,
        column: str,
        symbol: str,
        *,
        where: Mapping[str, Any] | None = None,
    ) -> date | datetime | None: ...


class ScopeProvider(Protocol):
    """Resolver for intraday_scope."""

    def __call__(self, symbol: str, interval: str) -> bool: ...


class GapProvider(Protocol):
    """Returns open (unresolved) gaps."""

    def __call__(self, symbol: str, interval: str) -> list[tuple[datetime, datetime]]: ...


# How a dataset responds to a --start/--end range.
#   "api"    : range passes through to the yfinance call -> a real backfill
#   "filter" : source returns a fixed window; normalize filters rows
#   "none"   : range is meaningless; the dataset does not run when --start is given
#
# The third level is required: --start acts on both `history` (a different
# fetch) and `upgrades_downgrades` (row filtering) in the same command. A
# single bool would make `--start 2020-01-01 --datasets history` fetch
# everything before 2020 from Yahoo and then discard it.
DateRange = Literal["api", "filter", "none"]


@dataclass(frozen=True)
class NormalizedResult:
    writes: list[TableWrite] = field(default_factory=list)
    # Not written because the hash was unchanged (table -> count)
    skipped: dict[str, int] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not any(w.rows for w in self.writes) and not self.skipped


def _sum_counters(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    """Sums per key; does not drop a zero-valued entry.

    Dropping zeros looks tempting but is wrong: when the hash matches,
    `AsOfGate.upsert` writes `attempted.setdefault(table, 0)` for a target
    with no rows. Losing that entry would drop the table from `tables()`,
    `_record_items` would never see it, and it would fall out of auditing
    for that run.
    """
    merged = dict(left)
    for name, count in right.items():
        merged[name] = merged.get(name, 0) + count
    return merged


def merge_stats(left: WriteStats, right: WriteStats) -> WriteStats:
    """Reduces two `WriteStats` into one.

    `DiscoveryDataset.upsert` splits its writes in two -- ungated tables
    (`symbols`, `news`, `news_symbols`, `research_reports`) get a plain
    upsert, the rest are delegated to `AsOfGate` -- producing two separate
    stats objects. `_record_items` expects a single `WriteStats`.

    Inputs are not mutated: the same dicts may still be in use during
    `apply_write` calls.
    """
    return WriteStats(
        attempted=_sum_counters(left.attempted, right.attempted),
        verified=_sum_counters(left.verified, right.verified),
        skipped=_sum_counters(left.skipped, right.skipped),
    )


class SyncContext:
    """Per-symbol context, owned by one worker.

    Not shared, so `cached` needs no lock; discarded entirely once the
    symbol is processed.
    """

    def __init__(
        self,
        symbol: str,
        ticker: Any,
        fetched_at: datetime,
        *,
        watermark_provider: WatermarkProvider | None = None,
        scope_provider: ScopeProvider | None = None,
        gap_provider: GapProvider | None = None,
        full_refresh: bool = False,
        selected: frozenset[str] | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> None:
        self.symbol = symbol
        self.ticker = ticker
        self.fetched_at = fetched_at
        self.full_refresh = full_refresh
        # --start/--end. None/None = unfiltered: all history Yahoo returns
        # is written. A range never fetches more data, only narrows scope;
        # for "api" datasets it overrides the watermark.
        self.start = start
        self.end = end
        # Dataset names selected for this run. The shared history frame's
        # `start` is the minimum of the watermarks of the tables consuming
        # that frame; None means "assume all selected" (the safe default
        # for library use and tests).
        self.selected = selected
        self._cache: dict[str, Any] = {}
        self._watermark_provider = watermark_provider
        self._scope_provider = scope_provider
        self._gap_provider = gap_provider

    def cached(self, key: str, fn: Callable[[], Any]) -> Any:
        """Fetch cache: symbols, fast_info, and history_metadata share the
        same two calls; capital_gains comes from the same cache as history."""
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def watermark(
        self,
        table: str,
        column: str,
        *,
        where: Mapping[str, Any] | None = None,
    ) -> date | datetime | None:
        """Read-only DB query; the contract explicitly allows this inside
        fetch. --full-refresh skips watermarks.

        `where` adds equality conditions. Required for price_bars: a single
        MAX(ts_utc) would mix 1m with 60m and 60m would be mistaken for
        up to date, so its first fill would never run. Omitting it keeps
        behavior identical to before.
        """
        if self.full_refresh or self._watermark_provider is None:
            return None
        return self._watermark_provider(table, column, self.symbol, where=where)

    def in_scope(self, interval: str) -> bool:
        """Whether the symbol is in scope for this interval.

        True when there is no provider: the safe default for library use
        and tests, since otherwise a directly invoked dataset would
        silently do nothing.
        """
        if self._scope_provider is None:
            return True
        return self._scope_provider(self.symbol, interval)

    def open_gaps(self, interval: str) -> list[tuple[datetime, datetime]]:
        """Unresolved gaps for this symbol/interval."""
        if self._gap_provider is None:
            return []
        return self._gap_provider(self.symbol, interval)


class Dataset[RawT](ABC):
    """Fetching, normalizing, and writing a single yfinance API.

    `RawT` is the type fetch returns; normalize takes the same type, so
    mypy can verify the contract between the two steps.
    """

    # `name` is declared as a class attribute, but a subtype may turn it
    # into an instance attribute (IntervalBarDataset uses one class for six
    # intervals). The Registrable protocol only requires `name: str`, so
    # both are valid and the contract does not narrow -- unlike `produces`,
    # where the type itself would change.
    name: str
    depends_on: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()  # table names it writes
    # A class attribute, not a property: turning it into a property in a
    # subtype would narrow the base contract, same issue as `produces`.
    date_range: DateRange = "none"
    # Opt-in to the generic read surface, one entry per readable
    # resource. Empty means "not served": the dataset stays out of the
    # catalogue and /v1/datasets/{name} answers 404. Failing closed is
    # what keeps a newly registered dataset from becoming readable, or
    # readable under the wrong scope, by accident. A tuple rather than a
    # single value because a dataset can write several tables and each is
    # its own resource.
    api: tuple[ApiExposure, ...] = ()

    @abstractmethod
    def fetch(self, ctx: SyncContext) -> RawT:
        """Fetches raw data.

        May raise `DatasetOutOfScope`: means the dataset was deliberately
        not run for that symbol, and the runner catches it before the
        generic error path and records it as OUT_OF_SCOPE. Part of the
        contract; subtypes are free to use it.
        """
        ...

    @abstractmethod
    def normalize(self, raw: RawT, symbol: str) -> NormalizedResult: ...

    def upsert(self, writer: RowWriter, result: NormalizedResult) -> WriteStats:
        """Default implementation: idempotent upsert plus key-existence
        verification for each TableWrite."""
        stats = WriteStats(skipped=dict(result.skipped))
        for write in result.writes:
            apply_write(writer, write, stats)
        return stats
