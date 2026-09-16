"""Dataset contract.

Independent of SQLAlchemy: which data goes to which table with which keys.
How the write happens lives in `yfin.storage.persistence`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal, Protocol

from yfin.datasets.common import mark_known
from yfin.datasets.exposure import ApiExposure
from yfin.storage.contracts import (
    RowWriter,
    SymbolLookup,
    TableWrite,
    WriteStats,
    apply_write,
)


class WatermarkProvider(Protocol):
    """Contract for a read-only watermark provider.

    A Protocol rather than `Callable[..., ...]` so call sites are
    type-checked; `where` is keyword-only and optional.
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

    `AsOfGate.upsert` records zero counts for a row-less target to keep it
    in `tables()` and therefore in auditing.
    """
    merged = dict(left)
    for name, count in right.items():
        merged[name] = merged.get(name, 0) + count
    return merged


def merge_stats(left: WriteStats, right: WriteStats) -> WriteStats:
    """Reduces two `WriteStats` into one; inputs are not mutated.

    Needed where a dataset splits its writes between a plain upsert and
    `AsOfGate`, since `_record_items` expects a single `WriteStats`.
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
        """Read-only DB query, allowed inside fetch; --full-refresh skips it.

        `where` adds equality conditions; price_bars needs it so intervals
        are not mixed in one MAX(ts_utc).
        """
        if self.full_refresh or self._watermark_provider is None:
            return None
        return self._watermark_provider(table, column, self.symbol, where=where)

    def in_scope(self, interval: str) -> bool:
        """Whether the symbol is in scope for this interval.

        True with no provider, so a directly invoked dataset does not
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



def plain_upsert(writer: RowWriter, result: NormalizedResult) -> WriteStats:
    """The write policy of a dataset with no gate: write everything.

    A module function because it is the default on both `Dataset` and
    `GlobalDataset`, which share no base.
    """
    stats = WriteStats(skipped=dict(result.skipped))
    for write in result.writes:
        apply_write(writer, write, stats)
    return stats



def mark_known_in(
    writer: SymbolLookup,
    result: NormalizedResult,
    *,
    select: Callable[[TableWrite], bool],
    column: str = "symbol",
) -> NormalizedResult:
    """`result` again, with `is_known` filled on the writes `select` picks.

    Marked writes come back in order, so position splices them back
    without relying on `id()` identity.
    """
    chosen = [select(write) for write in result.writes]
    targets = [write for write, take in zip(result.writes, chosen, strict=True) if take]
    if not targets:
        return result
    flagged = iter(mark_known(writer, targets, column=column))
    return NormalizedResult(
        writes=[
            next(flagged) if take else write
            for write, take in zip(result.writes, chosen, strict=True)
        ],
        skipped=dict(result.skipped),
    )


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
    # Opt-in to the generic read surface, one entry per readable table.
    # Empty means "not served": the dataset stays out of the catalogue and
    # /v1/datasets/{name} answers 404, so nothing becomes readable by accident.
    api: tuple[ApiExposure, ...] = ()
    # (table, date column) when this dataset consumes the shared `history()`
    # frame via `ctx.cached`. That call's `start` is the MINIMUM of the
    # watermarks declared here; a consumer that does not declare one fills
    # from a window narrowed by its siblings.
    shared_frame_watermark: tuple[str, str] | None = None

    @abstractmethod
    def fetch(self, ctx: SyncContext) -> RawT:
        """Fetches raw data.

        May raise `DatasetOutOfScope`; the runner records it as OUT_OF_SCOPE
        instead of an error.
        """
        ...

    @abstractmethod
    def normalize(self, raw: RawT, symbol: str) -> NormalizedResult: ...

    def upsert(
        self, writer: RowWriter, result: NormalizedResult, *, full_refresh: bool = False
    ) -> WriteStats:
        """Default implementation: `plain_upsert`.

        `full_refresh` is ignored by an ungated dataset but stays in the
        base signature because `persist_symbol` passes it to every dataset.
        """
        return plain_upsert(writer, result)
