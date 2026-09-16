"""Coordinator: one OS process per proxy, a dynamic symbol queue.

yfinance's config, `YfData` and token bucket are process-global, so
threads cannot use different proxies; rotation pivots on process.
"""

from __future__ import annotations

import multiprocessing as mp
import signal
import types
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from queue import Empty
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from yfin.core.config import (
    Settings,
    applied_overrides,
    get_settings,
    install_settings,
    settings_from_overrides,
)
from yfin.core.logging_setup import configure_logging, get_logger
from yfin.core.metrics import Accumulator, use_accumulator
from yfin.datasets import SYMBOL_DATASETS
from yfin.ingest.client import configure_yfinance
from yfin.pipeline import run_metrics
from yfin.pipeline.audit import RunTally, finalize_run, open_run, record_not_attempted
from yfin.pipeline.proxy_plan import (
    ProxyPlan,
    build_plans,
    eligible_proxies,
    record_crashes,
    tracker_for,
)
from yfin.pipeline.runner import SymbolSource, list_source, run_shard
from yfin.storage.db import advisory_lock, create_db_engine, session_factory

if TYPE_CHECKING:
    from multiprocessing.queues import Queue as MPQueue

log = get_logger(__name__)

# End-of-queue marker. Each shard consumes one.
SENTINEL = "\x00"

# Child's own pool: there are two real concurrent consumers (WatermarkReader
# serializes reads with its own lock, plus the main thread's symbol
# transaction).
CHILD_POOL_SIZE = 3


@dataclass(frozen=True)
class ShardSpec:
    """Everything that crosses the process boundary. Must be picklable.

    Defined outside `__main__`: a spawn-started child's target and arguments
    must be importable.
    """

    run_id: int
    shard_index: int
    dataset_names: tuple[str, ...]
    full_refresh: bool
    database: str | None
    proxy_id: int | None = None
    proxy_label: str | None = None
    # Resolved DSN; the password exists only in memory and on this pipe,
    # never in a log line (logging_setup.redact_credentials).
    proxy_dsn: str | None = None
    # --start/--end and the universe selector must cross the process
    # boundary, or the child silently runs without the range.
    start: date | None = None
    end: date | None = None
    selector: str | None = None
    # DB overrides resolved by the parent: every shard runs one consistent
    # snapshot, and the child never reads settings from a database other
    # than `spec.database`.
    settings_overrides: dict[str, str] = field(default_factory=dict)

    @property
    def proxy_key(self) -> str:
        """Cache-directory key for tz/cookie/ISIN caches.

        Keyed by proxy, not shard_index: a cookie minted against one
        egress IP must not be reused with another.
        """
        return f"proxy-{self.proxy_id}" if self.proxy_id is not None else "direct"


def _queue_source(queue: MPQueue[str]) -> SymbolSource:
    def _next() -> str | None:
        try:
            value = queue.get(timeout=1.0)
        except Empty:
            return None
        return None if value == SENTINEL else value

    return _next


def shard_main(spec: ShardSpec, queue: MPQueue[str]) -> None:
    """Child process entry point. Must be module-level (spawn)."""
    # The child never issues a SELECT to the DB: overrides resolved by the
    # parent travel via `spec`. A running sync thus uses one consistent
    # snapshot.
    settings = settings_from_overrides(spec.settings_overrides)
    install_settings(settings, spec.settings_overrides)

    # First step, and the child names ITSELF: a shard's lines are `sync`,
    # not the `scheduler` that started the process it was forked from.
    # `spawn` gives the child no logging configuration at all, so anything
    # logged before this would go to an unconfigured logger.
    configure_logging(settings.log_level, settings.log_format, "sync")

    datasets = SYMBOL_DATASETS.resolve(list(spec.dataset_names))
    configure_yfinance(spec.proxy_dsn, proxy_key=spec.proxy_key, settings=settings)

    engine = create_db_engine(settings, spec.database, pool_size=CHILD_POOL_SIZE)
    tracker = tracker_for(spec.proxy_id, settings)

    # From here on every `metrics.inc` in this process lands in memory. A
    # shard exits long before any scrape could reach it, so the counters go
    # to `run_metrics` on the way out and the exporter reads the table.
    accumulator = Accumulator()
    use_accumulator(accumulator)
    try:
        counters = run_shard(
            engine,
            _queue_source(queue),
            datasets,
            run_id=spec.run_id,
            shard_index=spec.shard_index,
            proxy_id=spec.proxy_id,
            proxy_label=spec.proxy_label,
            tracker=tracker,
            settings=settings,
            full_refresh=spec.full_refresh,
            start=spec.start,
            end=spec.end,
        )
        log.info(
            "shard finished",
            shard=spec.shard_index,
            proxy=spec.proxy_label or "direct",
            symbols=counters.symbols_seen,
            failed_cells=counters.cells_failed,
            withdrawn=counters.withdrawn,
        )
    finally:
        # In `finally`, so a shard that crashed still leaves the counters it
        # managed to collect -- those are the ones worth having. `flush`
        # swallows its own errors, so this cannot turn a failed run into a
        # failed process.
        run_metrics.flush(
            sessionmaker(bind=engine, expire_on_commit=False, future=True),
            spec.run_id,
            spec.shard_index,
            accumulator,
        )
        engine.dispose()


def _drain(queue: MPQueue[str]) -> list[str]:
    """Collect symbols left in the queue (sentinels discarded).

    Called after join(), not before; but the queue is drained regardless,
    otherwise a feeder thread would hang on a full queue.
    """
    leftover: list[str] = []
    while True:
        try:
            value = queue.get_nowait()
        except Empty:
            break
        except (OSError, ValueError):  # pragma: no cover - queue already closed
            break
        if value != SENTINEL:
            leftover.append(value)
    return leftover


def _terminate(processes: Sequence[Any]) -> None:
    for process in processes:
        if process.is_alive():
            process.terminate()
    for process in processes:
        process.join(timeout=5)
    for process in processes:
        if process.is_alive():  # pragma: no cover - last resort
            process.kill()
            process.join(timeout=5)


def run_sharded(
    engine: Engine,
    symbols: Sequence[str],
    dataset_names: Sequence[str],
    *,
    settings: Settings | None = None,
    full_refresh: bool = False,
    max_shards: int | None = None,
    no_proxy: bool = False,
    require_proxy: bool = False,
    database: str | None = None,
    start: date | None = None,
    end: date | None = None,
    selector: str | None = None,
) -> RunTally:
    """Advisory lock -> proxy selection -> run open -> shards -> finalize.

    Only the parent takes the lock, and holds it until every child has
    finished, so orphan children can never overlap a new run.
    """
    cfg = settings or get_settings()
    factory = session_factory(engine)

    with advisory_lock(engine):
        with factory() as session:
            eligible = eligible_proxies(
                session,
                settings=cfg,
                max_shards=max_shards,
                no_proxy=no_proxy,
                require_proxy=require_proxy,
            )
            # Endpoints are resolved under the lock; Proxy objects are
            # session-bound, while ShardSpec is plain data.
            specs_source = build_plans(eligible, cfg, require_proxy=require_proxy)
        shard_count = max(1, len(specs_source))
        run_id = open_run(
            factory,
            symbol_count=len(symbols),
            dataset_count=len(dataset_names),
            shard_count=shard_count,
            selector=selector,
        )

        if not specs_source:
            # Single shard, no proxy: no separate process needed. yfinance
            # config must still be set up in this process, and this process
            # is shard 0 for the counters the exporter republishes.
            configure_yfinance(None, proxy_key="direct", settings=cfg)
            use_accumulator(Accumulator())
            try:
                run_shard(
                    engine,
                    list_source(list(symbols)),
                    SYMBOL_DATASETS.resolve(list(dataset_names)),
                    run_id=run_id,
                    settings=cfg,
                    full_refresh=full_refresh,
                    start=start,
                    end=end,
                )
            finally:
                run_metrics.flush(factory, run_id, shard_index=0)
            return finalize_run(
                factory,
                run_id,
                symbol_count=len(symbols),
                dataset_count=len(dataset_names),
            )

        leftover = _spawn_and_wait(
            specs_source,
            symbols,
            dataset_names,
            run_id=run_id,
            settings=cfg,
            full_refresh=full_refresh,
            database=database,
            factory=factory,
            start=start,
            end=end,
        )
        if leftover:
            record_not_attempted(factory, run_id, leftover)

        return finalize_run(
            factory, run_id, symbol_count=len(symbols), dataset_count=len(dataset_names)
        )


def _spawn_and_wait(
    plans: Sequence[ProxyPlan],
    symbols: Sequence[str],
    dataset_names: Sequence[str],
    *,
    run_id: int,
    settings: Settings,
    full_refresh: bool,
    database: str | None,
    factory: sessionmaker[Session],
    start: date | None = None,
    end: date | None = None,
) -> list[str]:
    # spawn is chosen explicitly. Already the default on macOS/3.13; the
    # explicit choice is for Linux portability (whose 3.13 default is fork).
    # Shared sockets and curl_cffi's CFFI handles misbehave under fork.
    ctx = mp.get_context("spawn")
    queue: MPQueue[str] = ctx.Queue()
    for symbol in symbols:
        queue.put(symbol)
    for _ in plans:
        queue.put(SENTINEL)

    processes: list[Any] = []
    for index, plan in enumerate(plans):
        spec = ShardSpec(
            run_id=run_id,
            shard_index=index,
            dataset_names=tuple(dataset_names),
            full_refresh=full_refresh,
            database=database,
            proxy_id=plan.proxy_id,
            proxy_label=plan.proxy_label,
            proxy_dsn=plan.dsn,
            start=start,
            end=end,
            settings_overrides=applied_overrides(),
        )
        process = ctx.Process(
            target=shard_main, args=(spec, queue), name=f"yfin-shard-{index}", daemon=False
        )
        process.start()
        processes.append(process)

    cancelled = False
    previous = signal.getsignal(signal.SIGTERM)

    def _on_sigterm(_signum: int, _frame: types.FrameType | None) -> None:
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except ValueError:  # pragma: no cover - not on the main thread
        previous = None

    try:
        deadline = settings.yf_shard_timeout_seconds
        for process in processes:
            process.join(timeout=deadline)
    except KeyboardInterrupt:
        cancelled = True
        log.warning("cancelled; terminating the shards")
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)

    crashed = [
        (plan, process)
        for plan, process in zip(plans, processes, strict=True)
        if process.is_alive() or process.exitcode not in (0, None)
    ]
    _terminate(processes)

    # A dead/timed-out child cannot flush its own status; the parent writes
    # the verdict, through the same pure apply_outcome function, so policy
    # stays in one place. SHARD_CRASH triggers a cooldown regardless of
    # threshold.
    if crashed:
        record_crashes(factory, [plan for plan, _ in crashed], settings)

    leftover = _drain(queue)
    queue.cancel_join_thread()
    queue.close()
    if cancelled:
        log.warning("unprocessed symbols", count=len(leftover))
    return leftover
