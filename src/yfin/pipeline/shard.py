"""Coordinator: one OS process per proxy, a dynamic symbol queue.

Why a process? `yf.config` and `YfData` are process-global singletons in
yfinance (config.py:21-61, data.py:83): four worker threads can't use
different proxies at once, one overwrites another's proxy. So rotation must
pivot on PROCESS, not thread.

Side benefit: since `client._bucket` and `config._settings` are also
process-global, each shard gets its own token bucket, and the rate limit
becomes meaningful per proxy (per egress IP).
"""

from __future__ import annotations

import multiprocessing as mp
import signal
import types
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
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
from yfin.datasets import SYMBOL_DATASETS
from yfin.ingest.client import configure_yfinance
from yfin.models import Proxy
from yfin.pipeline.runner import (
    RunTally,
    SymbolSource,
    finalize_run,
    list_source,
    open_run,
    record_not_attempted,
    run_shard,
)
from yfin.proxy import (
    HealthEvent,
    PasswordUndecryptable,
    ProxyPolicy,
    ShardProxyTracker,
    count_all,
    endpoint_of,
    persist_event,
    select_eligible,
)
from yfin.storage.db import advisory_lock, create_db_engine

if TYPE_CHECKING:
    from multiprocessing.queues import Queue as MPQueue

log = get_logger(__name__)

# End-of-queue marker. Each shard consumes one.
SENTINEL = "\x00"

# Child's own pool: there are two real concurrent consumers (WatermarkReader
# serializes reads with its own lock, plus the main thread's symbol
# transaction). Invariant: (shard_count x 5) + 2 <= max_connections.
CHILD_POOL_SIZE = 3


class NoEligibleProxy(RuntimeError):
    """--require-proxy was given but no eligible proxy exists."""


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
    # --start/--end and the symbol universe selector. If these didn't cross
    # the process boundary while the proxy pool is full -- the default
    # path -- the child would run with `start=None`: "filter" datasets
    # wouldn't apply the range, "none" datasets wouldn't be skipped, and
    # the user would believe the range was applied. Same chain already
    # exists for `full_refresh`.
    start: date | None = None
    end: date | None = None
    selector: str | None = None
    # DB overrides already resolved by the parent. If the child called its
    # own `get_settings()` three problems would follow: (a) a `yfin config
    # set` racing in between would make shard-0 and shard-3 run with
    # different configuration, (b) N extra connections would open, (c) the
    # child would connect to `settings.db_name` and do its actual work in
    # `spec.database` -- i.e. read settings from a schema other than the
    # one `--database` redirected it to.
    settings_overrides: dict[str, str] = field(default_factory=dict)

    @property
    def proxy_key(self) -> str:
        """Cache-directory key for tz/cookie/ISIN caches.

        Not keyed by shard_index: since proxy selection is ordered by
        latency/health, shard-0 can be a different proxy on the next run,
        and a cookie minted against A's IP would then be used with B's
        egress IP. The cookie cache's PK is `strategy` (cache.py:314), so a
        shared file would have every shard overwrite the same two rows.
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

    # First step: structlog runs with cache_logger_on_first_use=True, and
    # module-level loggers cache configuration on first use. Logging before
    # this silently falls back to an unconfigured PrintLogger in the child.
    configure_logging(settings.log_level)

    datasets = SYMBOL_DATASETS.resolve(list(spec.dataset_names))
    configure_yfinance(spec.proxy_dsn, proxy_key=spec.proxy_key, settings=settings)

    engine = create_db_engine(settings, spec.database, pool_size=CHILD_POOL_SIZE)
    tracker = ShardProxyTracker(spec.proxy_id, ProxyPolicy.from_settings(settings))
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
        engine.dispose()


def _effective_shards(
    session: Session,
    *,
    settings: Settings,
    max_shards: int | None,
    no_proxy: bool,
    require_proxy: bool,
) -> list[Proxy]:
    """Eligible proxies; an empty list means a single, direct connection.

    Formula:
        shard_count = 1                              if --no-proxy
                    = max(1, min(N, |eligible|))      otherwise
    A shard is never opened without a proxy; the one exception is a
    single-shard direct connection when the pool is empty or has no
    eligible entries.
    """
    if no_proxy:
        if require_proxy:
            # The two together are meaningless, and silently letting
            # --no-proxy win would take on ban risk without telling the user.
            raise ValueError("--no-proxy and --require-proxy cannot be given together")
        if max_shards is not None and max_shards > 1:
            # Running N shards from the same egress IP multiplies effective
            # rate by N; the rate limit is defined per shard.
            log.warning("--no-proxy reduced the shard count to 1", requested=max_shards)
        return []

    limit = max_shards if max_shards is not None else settings.yf_max_shards
    eligible = select_eligible(session, max(1, limit))
    if eligible:
        return eligible

    total = count_all(session)
    if require_proxy:
        raise NoEligibleProxy(
            f"no eligible proxy ({total} rows in the pool); --require-proxy was given"
        )
    if total:
        # Silently connecting directly would take on ban risk without
        # telling the user.
        log.warning("the pool has proxies but none are eligible; connecting directly", total=total)
    else:
        log.info("proxy pool is empty; connecting directly")
    return []


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

    The advisory lock is taken only here; children never take it. It is
    held until every child has finished: otherwise, if the parent dies, the
    lock is released, orphan children keep writing, and a new cron trigger
    lands on the same rows.
    """
    cfg = settings or get_settings()
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    with advisory_lock(engine):
        with factory() as session:
            eligible = _effective_shards(
                session,
                settings=cfg,
                max_shards=max_shards,
                no_proxy=no_proxy,
                require_proxy=require_proxy,
            )
            # Endpoints are resolved under the lock; Proxy objects are
            # session-bound, while ShardSpec is plain data.
            specs_source = _build_specs(session, eligible, cfg)
            if require_proxy and eligible and not specs_source:
                # The SQL eligibility query found proxies, but none of their
                # passwords could be decrypted (typically: YF_PROXY_SECRET_KEY
                # rotated). `_effective_shards`'s check runs before this
                # point, so it's re-checked here; otherwise the flow falls
                # into the "no proxy" branch and the entire universe gets
                # fetched from the operator's own IP -- exactly the scenario
                # --require-proxy exists to prevent (and the exit code would
                # be 0/2 instead of 5).
                raise NoEligibleProxy(
                    f"none of the {len(eligible)} eligible proxies could be "
                    "decrypted; --require-proxy was given "
                    "(is YF_PROXY_SECRET_KEY correct?)"
                )

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
            # config must still be set up in this process.
            configure_yfinance(None, proxy_key="direct", settings=cfg)
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


@dataclass(frozen=True)
class _ProxyPlan:
    proxy_id: int
    proxy_label: str
    dsn: str | None
    error: str | None = None


def _build_specs(
    session: Session, eligible: Sequence[Proxy], settings: Settings
) -> list[_ProxyPlan]:
    plans: list[_ProxyPlan] = []
    for row in eligible:
        try:
            endpoint = endpoint_of(row, settings)
        except PasswordUndecryptable as exc:
            # Doesn't mark the proxy dead: reported explicitly to avoid a
            # wrong diagnosis, and it's just skipped for this run.
            log.error("could not decrypt the proxy password", proxy=row.label, error=str(exc))
            continue
        plans.append(_ProxyPlan(proxy_id=int(row.id), proxy_label=row.label, dsn=endpoint.dsn()))
    return plans


def _spawn_and_wait(
    plans: Sequence[_ProxyPlan],
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
        _record_crashes(factory, [plan for plan, _ in crashed], settings)

    leftover = _drain(queue)
    queue.cancel_join_thread()
    queue.close()
    if cancelled:
        log.warning("unprocessed symbols", count=len(leftover))
    return leftover


def _record_crashes(
    factory: sessionmaker[Session], plans: Sequence[_ProxyPlan], settings: Settings
) -> None:
    policy = ProxyPolicy.from_settings(settings)
    now = datetime.now(UTC)
    with factory() as session:
        for plan in plans:
            log.error("shard terminated unexpectedly", proxy=plan.proxy_label)
            persist_event(
                session,
                plan.proxy_id,
                HealthEvent.SHARD_CRASH,
                policy=policy,
                now=now,
                error="shard terminated or timed out",
            )
        session.commit()
