"""Orchestration: parallelism, queueing, transaction boundaries, error isolation."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import Engine

from yfin.core.config import Settings, get_settings
from yfin.core.errors import PROXY_FAULT_KINDS, DatasetOutOfScope, ErrorKind, classify_error
from yfin.core.logging_setup import bind_shard_context, get_logger
from yfin.core.metrics import Accumulator, use_accumulator
from yfin.datasets.base import Dataset, NormalizedResult, SyncContext
from yfin.datasets.registry import SYMBOL_DATASETS
from yfin.ingest.client import configure_yfinance, make_ticker
from yfin.models import (
    ItemStatus,
)
from yfin.pipeline import run_metrics
from yfin.pipeline.audit import (
    ItemRecord,
    RunTally,
    finalize_run,
    open_run,
    write_items,
)
from yfin.pipeline.contracts import ProxyTracker
from yfin.pipeline.payload import SymbolPayload
from yfin.pipeline.persist import mark_unknown, persist_with_retry
from yfin.pipeline.readers import GapReader, ScopeReader, WatermarkReader
from yfin.storage.changes import context_for
from yfin.storage.db import advisory_lock, session_factory

log = get_logger(__name__)

# Skip reason when --start/--end is given and a dataset has date_range="none".
SKIP_DATE_RANGE = "date_range=none"


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
    payload = SymbolPayload(symbol=symbol, resolved=False, full_refresh=full_refresh)
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
            payload.failures.append(
                (dataset.name, f"{type(exc).__name__}: {exc}", kind.value)
            )
            payload.error_kinds.append(kind)
            continue
        elapsed = int((time.perf_counter() - started) * 1000)
        payload.success_count += 1
        payload.results.append((dataset, result, _rows_of(result), elapsed))

    return payload


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


@dataclass
class ShardCounters:
    """Progress telemetry sent to the parent. Not authoritative.

    Run totals and the exit code are computed from the DB
    (`sync_run_items`) instead; otherwise the two sources could diverge.
    """

    shard_index: int = 0
    symbols_seen: int = 0
    cells_failed: int = 0
    withdrawn: bool = False


# --------------------------------------------------------------------------
# Run lifecycle
# --------------------------------------------------------------------------


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
    factory = session_factory(engine)
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

    changes = context_for(
        enabled=cfg.yf_changes_enabled,
        run_id=run_id,
        range_threshold=cfg.yf_changes_range_threshold,
    )

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
                with factory() as session:
                    mark_unknown(session, payload.symbol, cfg.yf_delist_threshold)
                    session.commit()
            continue

        # Attached here rather than in the worker: the consumer knows the
        # run, and a payload that never reaches this point (a crashed
        # worker) has nothing to publish anyway.
        payload.changes = changes
        emit(persist_with_retry(factory, payload, cfg.yf_txn_retry_attempts))

    producer.join(timeout=5)
    if tracker is not None:
        with factory() as session:
            tracker.flush(session)
        counters.withdrawn = bool(tracker.withdrawn)
    return counters


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

    # Not the shard's job here: this entry point is used directly by
    # `yfin discover` and by library callers, and without it yfinance keeps
    # its own default of `hide_exceptions=True`. An internal failure then
    # comes back as an empty result, the audit records EMPTY rather than
    # FAILED, and the run exits 0 -- "no data" and "the request blew up"
    # become indistinguishable, in the direction that reports success.
    configure_yfinance(settings=cfg)

    factory = session_factory(engine)
    run_id = open_run(
        factory,
        symbol_count=len(symbols),
        dataset_count=len(datasets),
        shard_count=1,
        selector=selector,
    )
    # The unsharded path is its own "shard 0". It has the same reason a
    # child does -- this process exits before a scrape reaches it -- and the
    # exporter sums over shards either way.
    use_accumulator(Accumulator())
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
    run_metrics.flush(factory, run_id, shard_index=0)
    return finalize_run(factory, run_id, symbol_count=len(symbols), dataset_count=len(datasets))
