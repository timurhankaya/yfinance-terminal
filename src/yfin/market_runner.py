"""Market sync orchestration.

No symbol loop: 6 datasets, ~20 requests typically. Parallelism, a
queue, and backpressure machinery would be unwarranted complexity here.

Transaction boundary is per turn (dataset x region): if
economic_calendar fails, splits_calendar stays written; if
market_summary's EUROPE turn fails, its US turn stays written.

Lock name is 'yfin_market_sync', distinct from symbol sync's
'yfin_sync', so the two commands can run concurrently.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from yfin.client import configure_yfinance
from yfin.config import Settings, get_settings
from yfin.datasets.market.base import GlobalDataset, MarketContext
from yfin.datasets.registry import MARKET_DATASETS
from yfin.db import advisory_lock
from yfin.errors import classify_error
from yfin.logging_setup import get_logger
from yfin.models import RunScope
from yfin.persistence import PostgresRowWriter
from yfin.proxy import (
    PasswordUndecryptable,
    ProxyPolicy,
    ShardProxyTracker,
    endpoint_of,
    select_eligible,
)
from yfin.runner import (
    ItemRecord,
    ProxyTracker,
    RunTally,
    _failed_records,
    _record_items,
    finalize_run,
    open_run,
    write_items,
)

log = get_logger(__name__)

MARKET_LOCK_NAME = "yfin_market_sync"
# Symbol field used in the audit record for region-less (global) datasets
GLOBAL_SCOPE_MARKER = "*"


def market_regions(settings: Settings | None = None) -> list[str]:
    """Regions from config, validated against the MarketRegion enum.

    An invalid region raises ValueError here rather than inside
    Market(...), so it's `failed`, not `empty`, and caught at the
    configuration stage.
    """
    from yfinance import MarketRegion

    cfg = settings or get_settings()
    # MarketRegion isn't a StrEnum: str(member) gives "MarketRegion.US"
    valid = {member.value for member in MarketRegion}
    regions = [r.strip().upper() for r in cfg.yf_market_regions.split(",") if r.strip()]
    unknown = [r for r in regions if r not in valid]
    if unknown:
        raise ValueError(f"gecersiz piyasa bolgesi: {', '.join(unknown)}")
    return regions


def default_window(settings: Settings | None = None) -> tuple[date, date]:
    cfg = settings or get_settings()
    today = datetime.now(UTC).date()
    return (
        today - timedelta(days=cfg.yf_calendar_lookback_days),
        today + timedelta(days=cfg.yf_calendar_lookahead_days),
    )


def _fail(dataset: GlobalDataset[Any], scope_label: str, exc: Exception) -> list[ItemRecord]:
    """One audit record per table for a failed turn.

    Reuses the symbol side's function; `_failed_records` takes the
    registry as a parameter so table names resolve from MARKET_DATASETS.
    """
    return _failed_records(
        scope_label, dataset.name, f"{type(exc).__name__}: {exc}", MARKET_DATASETS
    )


def _run_turn(
    factory: sessionmaker[Any],
    dataset: GlobalDataset[Any],
    mctx: MarketContext,
    scope_label: str,
    tracker: ProxyTracker | None = None,
) -> list[ItemRecord]:
    """One turn: fetch -> normalize -> upsert, in its own transaction."""
    started = time.perf_counter()
    try:
        raw = dataset.fetch(mctx)
        result = dataset.normalize(raw)
    except Exception as exc:  # noqa: BLE001 - (dataset x region) error boundary
        kind = classify_error(exc)
        log.warning(
            "market dataset failed",
            dataset=dataset.name,
            scope=scope_label,
            kind=kind.value,
            error=str(exc),
        )
        if tracker is not None:
            tracker.record_error(kind, str(exc))
        return _fail(dataset, scope_label, exc)
    if tracker is not None:
        tracker.record_success()

    fetched = sum(len(w.rows) for w in result.writes)
    duration = int((time.perf_counter() - started) * 1000)

    with factory() as session:
        try:
            stats = dataset.upsert(PostgresRowWriter(session), result)
            session.commit()
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            log.error("market turn failed", dataset=dataset.name, scope=scope_label, error=str(exc))
            return _fail(dataset, scope_label, exc)
    return _record_items(dataset, scope_label, stats, fetched, duration)


def run_market_sync(
    engine: Engine,
    datasets: Sequence[GlobalDataset[Any]],
    *,
    settings: Settings | None = None,
    start: date | None = None,
    end: date | None = None,
    acquire_lock: bool = True,
) -> RunTally:
    cfg = settings or get_settings()
    if acquire_lock:
        with advisory_lock(engine, MARKET_LOCK_NAME):
            return run_market_sync(
                engine, datasets, settings=cfg, start=start, end=end, acquire_lock=False
            )

    window_start, window_end = default_window(cfg)
    window_start = start or window_start
    window_end = end or window_end
    regions = market_regions(cfg)

    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    # Market datasets aren't symbol-oriented: queueing and sharding are
    # meaningless here (each dataset is already a single global call).
    # A proxy is still used -- one process, one proxy from the pool.
    proxy_id, proxy_label, tracker = _setup_proxy(factory, cfg)

    # symbol_count=0 is required: exit_code() only produces code 1 when
    # symbol_count is nonzero; writing the region count would silently
    # shift the "resolved symbol" semantics.
    run_id = open_run(
        factory,
        symbol_count=0,
        dataset_count=len(datasets),
        scope=RunScope.MARKET,
    )

    base_ctx = MarketContext(
        fetched_at=datetime.now(UTC),
        start=window_start,
        end=window_end,
    )

    items: list[ItemRecord] = []
    for dataset in datasets:
        if dataset.scope == "region":
            # Region loop is outside the dataset: sync_run_items
            # granularity naturally becomes (dataset x table x region)
            for region in regions:
                items.extend(
                    _run_turn(factory, dataset, base_ctx.for_region(region), region, tracker)
                )
        elif dataset.scope == "variant":
            # Screen loop is outside for the same reason as the region
            # loop. The variant list is read in its own short-lived
            # session and materialized: turn transactions (`_run_turn`)
            # open their own sessions, so holding a read session open
            # across the loop would waste a connection.
            with factory() as session:
                variants = list(dataset.variants(cfg, session))
            for variant in variants:
                items.extend(
                    _run_turn(factory, dataset, base_ctx.for_variant(variant), variant, tracker)
                )
        else:
            items.extend(_run_turn(factory, dataset, base_ctx, GLOBAL_SCOPE_MARKER, tracker))

    write_items(factory, run_id, items, proxy_id=proxy_id, proxy_label=proxy_label)
    if tracker is not None:
        with factory() as session:
            tracker.flush(session)
    return finalize_run(factory, run_id, symbol_count=0, dataset_count=len(datasets))


def _setup_proxy(
    factory: sessionmaker[Any], settings: Settings
) -> tuple[int | None, str | None, ProxyTracker | None]:
    """Pick one proxy from the pool and point yfinance at it.

    Falls back to a direct connection if none is eligible, same policy
    as the symbol side. A proxy whose password can't be decrypted is
    skipped and reported, not marked dead.
    """
    with factory() as session:
        for row in select_eligible(session, limit=1):
            try:
                endpoint = endpoint_of(row, settings)
            except PasswordUndecryptable as exc:
                log.error("proxy parolasi cozulemedi", proxy=row.label, error=str(exc))
                continue
            configure_yfinance(endpoint.dsn(), proxy_key=f"proxy-{row.id}", settings=settings)
            log.info("market sync proxy", proxy=row.label)
            return (
                int(row.id),
                row.label,
                ShardProxyTracker(int(row.id), ProxyPolicy.from_settings(settings)),
            )
    configure_yfinance(None, proxy_key="direct", settings=settings)
    log.info("market sync dogrudan baglaniliyor")
    return None, None, None
