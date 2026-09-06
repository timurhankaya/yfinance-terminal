"""Orchestration: parallelism, queueing, transaction boundaries, error isolation."""

from __future__ import annotations

import queue
import random
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Protocol

from sqlalchemy import Engine, and_, case, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from yfin.core.config import Settings, get_settings
from yfin.core.errors import PROXY_FAULT_KINDS, DatasetOutOfScope, ErrorKind, classify_error
from yfin.core.logging_setup import bind_shard_context, get_logger
from yfin.datasets.base import Dataset, NormalizedResult, SyncContext
from yfin.datasets.meta import DatasetMeta
from yfin.datasets.registry import SYMBOL_DATASETS, Registry
from yfin.ingest.client import make_ticker
from yfin.models import (
    GAP_FETCH_FAILED,
    Base,
    ItemStatus,
    RunScope,
    RunStatus,
    Symbol,
    SyncRun,
    SyncRunItem,
)
from yfin.storage.contracts import WriteStats
from yfin.storage.db import advisory_lock
from yfin.storage.persistence import PostgresRowWriter
from yfin.storage.rescale import apply_pending

log = get_logger(__name__)

# Exit codes.
EXIT_OK = 0
EXIT_NO_SYMBOL_RESOLVED = 1
EXIT_PARTIAL = 2
EXIT_ALL_FAILED = 3
EXIT_LOCK_NOT_ACQUIRED = 4
EXIT_NO_PROXY = 5  # --require-proxy given but no usable proxy

# Skip reason when --start/--end is given and a dataset has date_range="none".
SKIP_DATE_RANGE = "date_range=none"

# Skip reason for a cell outside intraday_scope.
SKIP_OUT_OF_SCOPE = "outside intraday_scope"


@dataclass
class ItemRecord:
    symbol: str
    dataset: str
    status: ItemStatus
    table_name: str | None = None
    rows_fetched: int = 0
    rows_written: int = 0
    rows_verified: int = 0
    rows_skipped: int = 0
    duration_ms: int | None = None
    error: str | None = None
    # Region axis for domain (sector/industry) cells. Stays NULL for symbol
    # and market cells: `market_runner` writes region into `symbol` instead,
    # and that is left as-is deliberately -- changing it would break
    # existing audit queries.
    region: str | None = None


@dataclass
class SymbolPayload:
    """Worker output: all normalized results for one symbol."""

    symbol: str
    resolved: bool
    results: list[tuple[Dataset[Any], NormalizedResult, int, int]] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)
    # Datasets excluded before running (name, reason). A third channel is
    # needed because these are neither results nor errors; dropping them
    # silently would make datasets vanish from the audit on a --start run.
    skipped: list[tuple[str, str]] = field(default_factory=list)
    # Datasets left out of scope (name, reason). Kept separate from
    # `skipped` because it maps to a different ItemStatus: skipped means
    # "excluded by content_hash/date_range", out_of_scope means "this
    # symbol was never targeted for this interval".
    out_of_scope: list[tuple[str, str]] = field(default_factory=list)
    error: str | None = None
    # For proxy health accounting. Processed on the consumer thread, so no
    # lock is needed on the tracker.
    error_kinds: list[ErrorKind] = field(default_factory=list)
    success_count: int = 0


@dataclass
class RunSummary:
    run_id: int | None
    items: list[ItemRecord]
    symbol_count: int
    dataset_count: int

    @property
    def failed(self) -> int:
        return sum(1 for i in self.items if i.status is ItemStatus.FAILED)

    @property
    def resolved_symbols(self) -> int:
        unresolved = {i.symbol for i in self.items if i.status is ItemStatus.UNKNOWN_SYMBOL}
        return len({i.symbol for i in self.items} - unresolved)

    def exit_code(self) -> int:
        """No failures -> 0 (ok/empty/skipped are all fine)."""
        if self.symbol_count and self.resolved_symbols == 0:
            return EXIT_NO_SYMBOL_RESOLVED
        if self.failed == 0:
            return EXIT_OK
        # Same exclusion set as RunTally.cells: never-attempted cells must
        # not count toward "everything attempted failed".
        excluded = {ItemStatus.UNKNOWN_SYMBOL, ItemStatus.OUT_OF_SCOPE}
        cells = [i for i in self.items if i.status not in excluded]
        if cells and self.failed == len(cells):
            return EXIT_ALL_FAILED
        return EXIT_PARTIAL

    def totals(self) -> dict[str, int]:
        return {
            "rows_fetched": sum(i.rows_fetched for i in self.items),
            "rows_written": sum(i.rows_written for i in self.items),
            "rows_verified": sum(i.rows_verified for i in self.items),
            "rows_skipped": sum(i.rows_skipped for i in self.items),
        }


class WatermarkReader:
    """Read-only watermark provider. Opens its own short session so worker
    threads never touch the main transaction."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory
        self._lock = threading.Lock()

    def __call__(
        self,
        table: str,
        column: str,
        symbol: str,
        *,
        where: Mapping[str, Any] | None = None,
    ) -> date | datetime | None:
        target = Base.metadata.tables[table]
        conditions = [target.c["symbol"] == symbol]
        # price_bars needs a separate watermark per interval.
        for name, value in (where or {}).items():
            conditions.append(target.c[name] == value)
        stmt = select(func.max(target.c[column])).where(and_(*conditions))
        with self._lock, self._factory() as session:
            result = session.execute(stmt).scalar_one_or_none()
        return result


class ScopeReader:
    """Resolves intraday_scope.

    SyncContext has no DB access and `_worker` builds a fresh SyncContext
    per symbol, so caching the scope query on ctx would mean ~5,000
    queries per run. This instance reads it once per run instead; each
    shard child process gets its own instance.
    """

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory
        self._lock = threading.Lock()
        self._cache: dict[str, frozenset[str] | None] = {}

    def _symbols_for(self, interval: str) -> frozenset[str] | None:
        """Scope set for an interval; None = the full universe.

        Applied per bar_interval, independent of `enabled`: the rule is
        "does at least one row exist for this interval?". An interval
        with only enabled=0 rows still counts as "has rows" and runs no
        symbols.
        """
        if interval in self._cache:
            return self._cache[interval]
        table = Base.metadata.tables["intraday_scope"]
        with self._lock, self._factory() as session:
            any_row = session.execute(
                select(func.count()).select_from(table).where(table.c["bar_interval"] == interval)
            ).scalar_one()
            value: frozenset[str] | None
            if not any_row:
                # No rows for 1m means no symbols (risk: 1.21B rows/year);
                # for other intervals it means the full universe.
                value = frozenset() if interval == "1m" else None
            else:
                rows = session.execute(
                    select(table.c["symbol"]).where(
                        table.c["bar_interval"] == interval, table.c["enabled"].is_(True)
                    )
                ).scalars()
                value = frozenset(rows)
        self._cache[interval] = value
        return value

    def __call__(self, symbol: str, interval: str) -> bool:
        allowed = self._symbols_for(interval)
        return True if allowed is None else symbol in allowed


class GapReader:
    """Reads open (unresolved) gaps.

    Without this feedback loop bar_gaps would just be a tombstone: if a
    middle slice fails and later slices succeed, the watermark moves past
    the gap and that window is never requested again.
    """

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory
        self._lock = threading.Lock()

    def __call__(self, symbol: str, interval: str) -> list[tuple[datetime, datetime]]:
        table = Base.metadata.tables["bar_gaps"]
        stmt = select(table.c["gap_start_utc"], table.c["gap_end_utc"]).where(
            table.c["symbol"] == symbol,
            table.c["bar_interval"] == interval,
            table.c["reason"] == GAP_FETCH_FAILED,
            table.c["resolved_at"].is_(None),
        )
        with self._lock, self._factory() as session:
            return [(row[0], row[1]) for row in session.execute(stmt)]


def _rows_of(result: NormalizedResult) -> int:
    return sum(len(w.rows) for w in result.writes)


def resolve_symbol(
    ctx: SyncContext, bootstrap: Dataset[Any]
) -> tuple[NormalizedResult | None, str | None, ErrorKind | None]:
    """RESOLVE step: fast_info + history_metadata -> symbols row.

    Returns (None, error) on failure and all of the symbol's datasets are
    skipped. normalize() is wrapped too: FastInfo is lazy and raises
    KeyError on attribute access for an invalid symbol.
    """
    try:
        raw = bootstrap.fetch(ctx)
        result = bootstrap.normalize(raw, ctx.symbol)
    except Exception as exc:  # noqa: BLE001 - this is the error boundary
        kind = classify_error(exc)
        log.warning("symbol resolve failed", symbol=ctx.symbol, kind=kind.value, error=str(exc))
        return None, f"{type(exc).__name__}: {exc}", kind
    if result.is_empty:
        # An empty result is not a network error; don't penalize the proxy.
        return None, "symbol could not be resolved: empty result", None
    return result, None, None


def _worker(
    symbol: str,
    datasets: Sequence[Dataset[Any]],
    bootstrap: Dataset[Any],
    fetched_at: datetime,
    watermarks: WatermarkReader,
    full_refresh: bool,
    start: date | None = None,
    end: date | None = None,
    scopes: ScopeReader | None = None,
    gaps: GapReader | None = None,
) -> SymbolPayload:
    """fetch + normalize (network and pure transform). DB writes happen on the main thread."""
    payload = SymbolPayload(symbol=symbol, resolved=False)
    ctx = SyncContext(
        symbol,
        make_ticker(symbol),
        fetched_at,
        watermark_provider=watermarks,
        scope_provider=scopes,
        gap_provider=gaps,
        full_refresh=full_refresh,
        selected=frozenset(d.name for d in datasets),
        start=start,
        end=end,
    )

    bootstrap_result, error, kind = resolve_symbol(ctx, bootstrap)
    if bootstrap_result is None:
        payload.error = error
        if kind is not None:
            payload.error_kinds.append(kind)
        return payload
    payload.resolved = True
    payload.success_count += 1
    payload.results.append((bootstrap, bootstrap_result, _rows_of(bootstrap_result), 0))

    ranged = start is not None or end is not None
    for dataset in datasets:
        if dataset.name == bootstrap.name:
            continue
        # A 'none' dataset is excluded before fetch when a range is given.
        # Running it silently would look like the range was applied;
        # skipping without a record would leave a hole in the audit.
        if ranged and dataset.date_range == "none":
            payload.skipped.append((dataset.name, SKIP_DATE_RANGE))
            continue
        started = time.perf_counter()
        try:
            raw = dataset.fetch(ctx)
            result = dataset.normalize(raw, symbol)
        except DatasetOutOfScope as exc:
            # Must come before the generic except: falling through to
            # classify_error would record it FAILED and pollute proxy
            # health accounting with an out-of-scope symbol.
            payload.out_of_scope.append((dataset.name, str(exc)))
            continue
        except Exception as exc:  # noqa: BLE001 - error boundary per (symbol, dataset)
            kind = classify_error(exc)
            log.warning(
                "dataset failed",
                symbol=symbol,
                dataset=dataset.name,
                kind=kind.value,
                error=str(exc),
            )
            payload.failures.append((dataset.name, f"{type(exc).__name__}: {exc}"))
            payload.error_kinds.append(kind)
            continue
        elapsed = int((time.perf_counter() - started) * 1000)
        payload.success_count += 1
        payload.results.append((dataset, result, _rows_of(result), elapsed))

    return payload


def _record_items(
    dataset: DatasetMeta,
    symbol: str,
    stats: WriteStats,
    fetched: int,
    duration_ms: int,
    *,
    region: str | None = None,
) -> list[ItemRecord]:
    """One row per table for datasets that write to multiple tables."""
    records: list[ItemRecord] = []
    # `or [None]`: without this, a tracking-only dataset (sustainability,
    # produces=()) writes no row at all and vanishes from the audit.
    tables: list[str | None] = list(stats.tables() or dataset.produces) or [None]
    for position, table in enumerate(tables):
        # table is None only for a tracking-only dataset (writes nothing);
        # all counters are 0 there and the cell is EMPTY.
        attempted = stats.attempted.get(table, 0) if table else 0
        verified = stats.verified.get(table, 0) if table else 0
        skipped = stats.skipped.get(table, 0) if table else 0
        if attempted == 0 and skipped == 0:
            status = ItemStatus.EMPTY  # no source data - not an error
        elif attempted == 0 and skipped:
            status = ItemStatus.SKIPPED  # content_hash unchanged
        elif verified == attempted:
            status = ItemStatus.OK
        else:
            status = ItemStatus.FAILED
        records.append(
            ItemRecord(
                symbol=symbol,
                dataset=dataset.name,
                status=status,
                table_name=table,
                region=region,
                # `fetched` is rows pulled for the whole dataset, not per
                # table. Writing it to every table row would inflate
                # sync_runs.rows_fetched for multi-table datasets (info: 3
                # tables, news: 2), so only the first table row gets it.
                rows_fetched=fetched if position == 0 else 0,
                rows_written=attempted,
                rows_verified=verified,
                rows_skipped=skipped,
                duration_ms=duration_ms,
                error=(
                    f"rows_verified({verified}) != rows_attempted({attempted})"
                    if status is ItemStatus.FAILED
                    else None
                ),
            )
        )
    return records


def _failed_records(
    symbol: str,
    dataset_name: str,
    error: str,
    registry: Registry[Any] = SYMBOL_DATASETS,
    *,
    region: str | None = None,
) -> list[ItemRecord]:
    """One record per table for a failed cell.

    A single table_name=NULL row would make audit queries unable to
    filter per table, so "when did this table last fail" is unanswerable.
    """
    dataset = registry.get(dataset_name)
    # Same gap exists on the error path: produces=() -> tuple(()) -> no
    # rows. `or (None,)` covers it.
    tables: tuple[str | None, ...] = (tuple(dataset.produces) if dataset else ()) or (None,)
    return [
        ItemRecord(
            symbol=symbol,
            dataset=dataset_name,
            status=ItemStatus.FAILED,
            table_name=table,
            error=error,
            region=region,
        )
        for table in tables
    ]


def _skipped_records(
    symbol: str,
    dataset_name: str,
    reason: str,
    registry: Registry[Any] = SYMBOL_DATASETS,
    status: ItemStatus = ItemStatus.SKIPPED,
    *,
    region: str | None = None,
) -> list[ItemRecord]:
    """One record per table for a cell excluded before running.

    Same per-table-row rule as `_failed_records`, differing only in that
    the status and reason are carried in the `error` field. `status` also
    covers OUT_OF_SCOPE so a separate function doesn't duplicate the body.
    """
    dataset = registry.get(dataset_name)
    tables: tuple[str | None, ...] = (tuple(dataset.produces) if dataset else ()) or (None,)
    return [
        ItemRecord(
            symbol=symbol,
            dataset=dataset_name,
            status=status,
            table_name=table,
            error=reason,
            region=region,
        )
        for table in tables
    ]


def _persist_symbol(session: Session, payload: SymbolPayload) -> list[ItemRecord]:
    """One transaction per symbol: written as a whole or not at all."""
    records: list[ItemRecord] = []
    writer = PostgresRowWriter(session)
    # Rescale hook runs before price_bars is written, in the same
    # transaction. In the reverse order, bars written in this run (already
    # at Yahoo's current scale) would get split again. A separate
    # transaction doesn't work either: _persist_with_retry replays the
    # whole block on a lock conflict, and a rescale that already committed
    # would muddy the accounting even if not reapplied.
    _rescale_before_bars(session, payload)
    for dataset, result, fetched, duration in payload.results:
        stats = dataset.upsert(writer, result)
        records.extend(_record_items(dataset, payload.symbol, stats, fetched, duration))
    for dataset_name, error in payload.failures:
        records.extend(_failed_records(payload.symbol, dataset_name, error))
    for dataset_name, reason in payload.skipped:
        records.extend(_skipped_records(payload.symbol, dataset_name, reason))
    for dataset_name, reason in payload.out_of_scope:
        records.extend(
            _skipped_records(
                payload.symbol, dataset_name, reason, status=ItemStatus.OUT_OF_SCOPE
            )
        )
    return records


def _rescale_before_bars(session: Session, payload: SymbolPayload) -> None:
    """Applies pending splits if any dataset writes to price_bars.

    Only runs when price_bars will actually be written, so a run like
    `--datasets info` doesn't needlessly query splits/bar_rescales.
    """
    writes_bars = any(
        "price_bars" in dataset.produces for dataset, _result, _f, _d in payload.results
    )
    if not writes_bars:
        return
    try:
        apply_pending(session, payload.symbol)
    except Exception as exc:  # noqa: BLE001 - a hook failure must not drop the symbol
        # Swallowing this is dangerous since we're in the same transaction:
        # a broken rescale would silently stick. Re-raise instead and let
        # _persist_with_retry and the caller handle it.
        log.error("rescale hook failed", symbol=payload.symbol, error=str(exc))
        raise


def _mark_unknown(session: Session, symbol: str, threshold: int) -> None:
    """A symbol unknown for 5 consecutive runs gets is_active=0. Data is not deleted."""
    row = session.get(Symbol, symbol)
    if row is None:
        # The universe is managed by hand; a symbol not on record has no
        # streak counter. Log it instead of staying silent.
        log.info("unknown symbol not tracked (not in symbols table)", symbol=symbol)
        return
    streak = (row.unknown_streak or 0) + 1
    session.execute(
        update(Symbol)
        .where(Symbol.symbol == symbol)
        .values(unknown_streak=streak, is_active=streak < threshold)
    )


# --------------------------------------------------------------------------
# Symbol source
# --------------------------------------------------------------------------

# A call asking what a shard should process next. None = source exhausted.
# Backed by a list for a single shard, by an mp.Queue for multiple shards;
# the runner doesn't distinguish between the two.
SymbolSource = Callable[[], str | None]


def list_source(symbols: Sequence[str]) -> SymbolSource:
    """List-backed source; safe to share across threads."""
    iterator = iter(symbols)
    lock = threading.Lock()

    def _next() -> str | None:
        with lock:
            return next(iterator, None)

    return _next


class ProxyTracker(Protocol):
    """The only interface the runner sees into proxy health accounting.

    The concrete implementation is `yfin.proxy.ShardProxyTracker`; the
    runner depends on this abstraction instead so proxy policy can evolve
    without touching the runner, and tests can pass a fake tracker.
    """

    withdrawn: bool

    def record_success(self) -> None: ...

    def record_error(self, kind: ErrorKind, message: str | None = None) -> None: ...

    def flush(self, session: Session) -> None: ...


@dataclass
class ShardCounters:
    """Progress telemetry sent to the parent. Not authoritative.

    Run totals and the exit code are computed from the DB
    (`sync_run_items`) instead; otherwise the two sources could diverge.
    """

    shard_index: int = 0
    symbols_seen: int = 0
    symbols_unresolved: int = 0
    cells_failed: int = 0
    withdrawn: bool = False


# --------------------------------------------------------------------------
# Run lifecycle
# --------------------------------------------------------------------------


def open_run(
    factory: sessionmaker[Session],
    *,
    symbol_count: int,
    dataset_count: int,
    shard_count: int = 1,
    scope: RunScope = RunScope.SYMBOLS,
    selector: str | None = None,
) -> int:
    """Opens a sync_runs row and commits it.

    The commit is required: children use a separate connection, and
    writing an item against an uncommitted run_id would raise an FK
    violation (23503).
    """
    with factory() as session:
        run = SyncRun(
            started_at=datetime.now(UTC),
            scope=scope,
            status=RunStatus.RUNNING,
            symbol_count=symbol_count,
            dataset_count=dataset_count,
            shard_count=shard_count,
            # Without this, which universe a run covered can't be
            # reconstructed later, and completeness can't be audited.
            selector=selector,
        )
        session.add(run)
        session.commit()
        return int(run.id)


# PostgreSQL SQLSTATEs. Symbols are spread across shards, so two processes
# can write the same news / news_symbols row; a single process had no such
# risk.
#   40001 serialization_failure
#   40P01 deadlock_detected
#
# 55P03 (lock_not_available) is deliberately not listed: it can't occur on
# this code path since NOWAIT / SKIP LOCKED are not used. Retrying an
# unjustified SQLSTATE would silently legitimize the wrong behavior if
# NOWAIT is ever added.
_RETRYABLE_SQLSTATES = frozenset({"40001", "40P01"})


def _is_lock_conflict(exc: BaseException) -> bool:
    """Checks SQLSTATE, not the error message.

    Text matching is affected by localized messages and driver formatting
    changes; SQLSTATE is structural and stable. psycopg3 exceptions carry
    `sqlstate`, and SQLAlchemy exposes it under `DBAPIError.orig`. An
    exception without `orig` (a programming error) makes the getattr
    chain return None and correctly skips the retry.
    """
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    return sqlstate in _RETRYABLE_SQLSTATES


def _persist_with_retry(
    factory: sessionmaker[Session], payload: SymbolPayload, attempts: int
) -> list[ItemRecord]:
    """Symbol transaction with jittered retry on a lock conflict.

    Safe to replay because the transaction is symbol-scoped and
    idempotent. In PostgreSQL a failed transaction always enters aborted
    state and accepts nothing but ROLLBACK, so the rollback before retry
    is not optional -- the engine enforces it.
    """
    last_error = ""
    for attempt in range(1, attempts + 1):
        with factory() as session:
            try:
                records = _persist_symbol(session, payload)
                session.commit()
                return records
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < attempts and _is_lock_conflict(exc):
                    delay = 0.05 * attempt + random.uniform(0, 0.05)
                    log.warning(
                        "lock conflict; retrying",
                        symbol=payload.symbol,
                        attempt=attempt,
                        error=last_error,
                    )
                    time.sleep(delay)
                    continue
                log.error("symbol transaction failed", symbol=payload.symbol, error=last_error)
                break
    records = [
        record
        for dataset, _, _, _ in payload.results
        for record in _failed_records(payload.symbol, dataset.name, last_error)
    ]
    # The other three channels stay in the audit too. They don't depend on
    # the write layer: `failures` blew up during fetch, `skipped` and
    # `out_of_scope` never hit the network at all. Dropping them would
    # leave no row for those cells -- not even `failed`, just absence --
    # and silently mislead "when was this dataset last attempted".
    for dataset_name, error in payload.failures:
        records.extend(_failed_records(payload.symbol, dataset_name, error))
    for dataset_name, reason in payload.skipped:
        records.extend(_skipped_records(payload.symbol, dataset_name, reason))
    for dataset_name, reason in payload.out_of_scope:
        records.extend(
            _skipped_records(
                payload.symbol, dataset_name, reason, status=ItemStatus.OUT_OF_SCOPE
            )
        )
    return records


def write_items(
    factory: sessionmaker[Session],
    run_id: int,
    records: Sequence[ItemRecord],
    *,
    shard_index: int = 0,
    proxy_id: int | None = None,
    proxy_label: str | None = None,
) -> None:
    """Writes audit records in a separate transaction.

    In the same transaction as the symbol write, a rollback would erase
    the audit trail too -- exactly when a record of the failure matters.
    """
    if not records:
        return
    with factory() as session:
        session.add_all(
            SyncRunItem(
                run_id=run_id,
                symbol=item.symbol,
                dataset=item.dataset,
                status=item.status,
                table_name=item.table_name,
                rows_fetched=item.rows_fetched,
                rows_written=item.rows_written,
                rows_verified=item.rows_verified,
                rows_skipped=item.rows_skipped,
                duration_ms=item.duration_ms,
                error=item.error,
                region=item.region,
                shard_index=shard_index,
                proxy_id=proxy_id,
                proxy_label=proxy_label,
            )
            for item in records
        )
        session.commit()


def run_shard(
    engine: Engine,
    source: SymbolSource,
    datasets: Sequence[Dataset[Any]],
    *,
    run_id: int,
    shard_index: int = 0,
    proxy_id: int | None = None,
    proxy_label: str | None = None,
    tracker: ProxyTracker | None = None,
    settings: Settings | None = None,
    full_refresh: bool = False,
    start: date | None = None,
    end: date | None = None,
) -> ShardCounters:
    """One shard: pulls symbols off the queue, processes them, writes audit records.

    The parallelism axis is the symbol; a shard is just an extra layer of
    process fan-out around it.
    """
    cfg = settings or get_settings()
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    watermarks = WatermarkReader(factory)
    # Scope set is read once per run; open gaps are queried per symbol
    # since they're symbol-specific.
    scopes = ScopeReader(factory)
    gaps = GapReader(factory)
    bootstrap = next(d for d in datasets if d.name == SYMBOL_DATASETS.bootstrap)
    counters = ShardCounters(shard_index=shard_index)

    # The queue is bounded by maxsize. For the bound to actually apply
    # backpressure, the worker itself puts the result onto the queue: when
    # full, the worker blocks on put() and pulls no new symbol.
    results: queue.Queue[SymbolPayload | None] = queue.Queue(maxsize=cfg.yf_queue_maxsize)

    def worker_loop() -> None:
        # contextvars are not copied into ThreadPoolExecutor workers, so
        # the binding is redone at the start of each thread.
        bind_shard_context(run_id, shard_index, proxy_label)
        while True:
            # If the proxy entered cooldown, the shard withdraws itself.
            # Otherwise a banned proxy hits 429 immediately, drains the
            # most symbols off the queue, and marks all of them failed.
            if tracker is not None and tracker.withdrawn:
                counters.withdrawn = True
                return
            symbol = source()
            if symbol is None:
                return
            try:
                payload = _worker(
                    symbol,
                    datasets,
                    bootstrap,
                    # fetched_at is generated once per symbol in Python,
                    # not via a DB function.
                    datetime.now(UTC),
                    watermarks,
                    full_refresh,
                    start=start,
                    end=end,
                    scopes=scopes,
                    gaps=gaps,
                )
            except Exception as exc:  # noqa: BLE001
                log.error("worker crashed", symbol=symbol, error=str(exc))
                payload = SymbolPayload(
                    symbol=symbol, resolved=False, error=f"{type(exc).__name__}: {exc}"
                )
            results.put(payload)  # blocks here if the queue is full

    def produce() -> None:
        try:
            with ThreadPoolExecutor(max_workers=cfg.yf_max_workers) as pool:
                futures = [pool.submit(worker_loop) for _ in range(cfg.yf_max_workers)]
                for future in futures:
                    future.result()
        except BaseException as exc:  # noqa: BLE001
            log.error("producer crashed", error=f"{type(exc).__name__}: {exc}")
        finally:
            # The sentinel must always be written; otherwise, if the
            # executor raises unexpectedly, the consumer blocks forever on
            # results.get() (the queue has no timeout).
            results.put(None)

    producer = threading.Thread(target=produce, name=f"yfin-producer-{shard_index}", daemon=True)
    producer.start()

    def emit(records: Sequence[ItemRecord]) -> None:
        write_items(
            factory,
            run_id,
            records,
            shard_index=shard_index,
            proxy_id=proxy_id,
            proxy_label=proxy_label,
        )
        counters.cells_failed += sum(1 for r in records if r.status is ItemStatus.FAILED)

    while True:
        payload = results.get()
        if payload is None:
            break
        counters.symbols_seen += 1

        # Proxy health accounting happens on a single thread, so the
        # tracker needs no lock.
        if tracker is not None:
            for kind in payload.error_kinds:
                tracker.record_error(kind)
            if payload.success_count:
                tracker.record_success()
            if tracker.withdrawn:
                with factory() as session:
                    tracker.flush(session)

        if not payload.resolved:
            # A transport error is not the symbol's fault. No symbol
            # resolves on a dead proxy; counting these as unknown_symbol
            # would run out the delist streak and silently set is_active=0
            # for the whole universe once yf_delist_threshold is hit.
            transport_fault = any(k in PROXY_FAULT_KINDS for k in payload.error_kinds)
            status = ItemStatus.FAILED if transport_fault else ItemStatus.UNKNOWN_SYMBOL
            emit(
                [
                    ItemRecord(
                        symbol=payload.symbol,
                        dataset=SYMBOL_DATASETS.bootstrap or "symbols",
                        status=status,
                        error=payload.error or "symbol could not be resolved",
                    )
                ]
            )
            if not transport_fault:
                counters.symbols_unresolved += 1
                with factory() as session:
                    _mark_unknown(session, payload.symbol, cfg.yf_delist_threshold)
                    session.commit()
            continue

        emit(_persist_with_retry(factory, payload, cfg.yf_txn_retry_attempts))

    producer.join(timeout=5)
    if tracker is not None:
        with factory() as session:
            tracker.flush(session)
        counters.withdrawn = bool(tracker.withdrawn)
    return counters


def record_not_attempted(
    factory: sessionmaker[Session], run_id: int, symbols: Sequence[str]
) -> None:
    """Symbols left unprocessed in the queue.

    Without these rows, those symbols would have no sync_run_items record
    at all, failed would stay zero, and the run could return 'ok' + exit 0
    while half the universe was never fetched.
    """
    write_items(
        factory,
        run_id,
        [
            ItemRecord(
                symbol=symbol,
                dataset=SYMBOL_DATASETS.bootstrap or "symbols",
                status=ItemStatus.NOT_ATTEMPTED,
                error="shard withdrawn; symbol left in the queue",
            )
            for symbol in symbols
        ],
    )


@dataclass
class RunTally:
    """The single source of truth for a run's result: sync_run_items aggregation."""

    run_id: int
    symbol_count: int
    dataset_count: int
    resolved_symbols: int
    counts: dict[str, int] = field(default_factory=dict)
    totals: dict[str, int] = field(default_factory=dict)

    @property
    def failed(self) -> int:
        return self.counts.get(ItemStatus.FAILED.value, 0)

    @property
    def not_attempted(self) -> int:
        return self.counts.get(ItemStatus.NOT_ATTEMPTED.value, 0)

    @property
    def cells(self) -> int:
        """Cells excluding unknown_symbol, not_attempted, and out_of_scope.

        out_of_scope is excluded too: never-attempted cells must not count
        toward "everything attempted failed". If 4,500 out-of-scope cells
        counted, EXIT_PARTIAL would return instead of EXIT_ALL_FAILED even
        if all 500 genuinely attempted cells failed.
        """
        excluded = {
            ItemStatus.UNKNOWN_SYMBOL.value,
            ItemStatus.NOT_ATTEMPTED.value,
            ItemStatus.OUT_OF_SCOPE.value,
        }
        return sum(v for k, v in self.counts.items() if k not in excluded)

    def exit_code(self) -> int:
        """No failures -> 0 (ok/empty/skipped are all fine).

        The exit code can never be 0 while an unprocessed symbol remains.
        """
        if self.symbol_count and self.resolved_symbols == 0:
            return EXIT_NO_SYMBOL_RESOLVED
        if self.failed == 0:
            return EXIT_PARTIAL if self.not_attempted else EXIT_OK
        if self.cells and self.failed == self.cells:
            return EXIT_ALL_FAILED
        return EXIT_PARTIAL


_STATUS_TO_RUN = {
    EXIT_OK: RunStatus.OK,
    EXIT_PARTIAL: RunStatus.PARTIAL,
    EXIT_ALL_FAILED: RunStatus.FAILED,
    EXIT_NO_SYMBOL_RESOLVED: RunStatus.FAILED,
}


def finalize_run(
    factory: sessionmaker[Session],
    run_id: int,
    *,
    symbol_count: int,
    dataset_count: int,
) -> RunTally:
    """Computes totals and the exit code from the DB, closes out sync_runs.

    The summary is not carried to the parent via the result queue: if
    sync_runs totals came from the queue while sync_run_items reflects
    what the children actually did, the two could diverge and undermine
    the "machine-verifiable completeness" guarantee.

    The caller must have joined all children first, or this aggregates
    over incomplete data.
    """
    with factory() as session:
        rows = session.execute(
            select(
                SyncRunItem.status,
                func.count(),
                func.coalesce(func.sum(SyncRunItem.rows_fetched), 0),
                func.coalesce(func.sum(SyncRunItem.rows_written), 0),
                func.coalesce(func.sum(SyncRunItem.rows_verified), 0),
                func.coalesce(func.sum(SyncRunItem.rows_skipped), 0),
            )
            .where(SyncRunItem.run_id == run_id)
            .group_by(SyncRunItem.status)
        ).all()

        counts: dict[str, int] = {}
        totals = {
            "rows_fetched": 0,
            "rows_written": 0,
            "rows_verified": 0,
            "rows_skipped": 0,
        }
        for status, count, fetched, written, verified, skipped in rows:
            counts[ItemStatus(status).value] = int(count)
            totals["rows_fetched"] += int(fetched)
            totals["rows_written"] += int(written)
            totals["rows_verified"] += int(verified)
            totals["rows_skipped"] += int(skipped)

        # A resolved symbol is one with no unknown_symbol row at all.
        unresolved_flag = case((SyncRunItem.status == ItemStatus.UNKNOWN_SYMBOL, 1), else_=0)
        resolved_subq = (
            select(SyncRunItem.symbol)
            .where(SyncRunItem.run_id == run_id)
            .group_by(SyncRunItem.symbol)
            .having(func.sum(unresolved_flag) == 0)
            .subquery()
        )
        resolved = int(
            session.execute(select(func.count()).select_from(resolved_subq)).scalar_one()
        )

        # Reconciliation: a symbol with no row at all in the audit is one a
        # crashed or timed-out shard pulled off the queue but never
        # finished. `shard.py` only drains what's still in the queue, so
        # the symbol a child was holding gets no `sync_run_items` row.
        # Without counting this gap, aggregation would mistake incomplete
        # data for complete and return EXIT_OK -- exactly where the
        # "exit code can never be 0 with an unprocessed symbol" guarantee
        # would break. symbol_count=0 for market runs, so this branch is a
        # no-op there.
        covered = int(
            session.execute(
                select(func.count(func.distinct(SyncRunItem.symbol))).where(
                    SyncRunItem.run_id == run_id
                )
            ).scalar_one()
        )
        missing = max(0, symbol_count - covered)
        if missing:
            log.error(
                "run reconciliation: symbols missing from audit",
                run_id=run_id,
                expected=symbol_count,
                covered=covered,
                missing=missing,
            )
            counts[ItemStatus.NOT_ATTEMPTED.value] = (
                counts.get(ItemStatus.NOT_ATTEMPTED.value, 0) + missing
            )

        tally = RunTally(
            run_id=run_id,
            symbol_count=symbol_count,
            dataset_count=dataset_count,
            resolved_symbols=resolved,
            counts=counts,
            totals=totals,
        )
        session.execute(
            update(SyncRun)
            .where(SyncRun.id == run_id)
            .values(
                finished_at=datetime.now(UTC),
                status=_STATUS_TO_RUN[tally.exit_code()],
                **totals,
            )
        )
        session.commit()
    return tally


def run_sync(
    engine: Engine,
    symbols: Sequence[str],
    datasets: Sequence[Dataset[Any]],
    *,
    settings: Settings | None = None,
    full_refresh: bool = False,
    acquire_lock: bool = True,
    start: date | None = None,
    end: date | None = None,
    selector: str | None = None,
) -> RunTally:
    """Single-shard, proxy-less run (backward-compatible entry point).

    The advisory lock is acquired here, not in the CLI layer, so that two
    concurrent runs can't write the same rows even when run_sync is
    called directly (library use, live tests). `acquire_lock` is turned
    off only for a caller that already holds the lock externally.

    For multi-shard runs, see `yfin.pipeline.shard.run_sharded`.
    """
    cfg = settings or get_settings()
    if acquire_lock:
        with advisory_lock(engine):
            return run_sync(
                engine,
                symbols,
                datasets,
                settings=cfg,
                full_refresh=full_refresh,
                acquire_lock=False,
                start=start,
                end=end,
                selector=selector,
            )

    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    run_id = open_run(
        factory,
        symbol_count=len(symbols),
        dataset_count=len(datasets),
        shard_count=1,
        selector=selector,
    )
    run_shard(
        engine,
        list_source(symbols),
        datasets,
        run_id=run_id,
        settings=cfg,
        full_refresh=full_refresh,
        start=start,
        end=end,
    )
    return finalize_run(factory, run_id, symbol_count=len(symbols), dataset_count=len(datasets))
