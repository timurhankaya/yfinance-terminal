"""Market sync orchestration: one process, one proxy, no symbol loop.

Transaction boundary is one turn (dataset x region), so one failing turn
leaves the others written.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from yfin.core.config import Settings, get_settings
from yfin.core.logging_setup import get_logger
from yfin.core.metrics import Accumulator, use_accumulator
from yfin.core.text import comma_list
from yfin.datasets.axis import DatasetAxis
from yfin.datasets.market.base import GlobalDataset, MarketContext, MarketScope
from yfin.datasets.registry import MARKET_DATASETS
from yfin.models import RunScope
from yfin.pipeline import run_metrics
from yfin.pipeline.audit import (
    ItemRecord,
    RunTally,
    finalize_run,
    open_run,
    write_items,
)
from yfin.pipeline.contracts import ProxyTracker
from yfin.pipeline.single_proxy import setup_single_proxy
from yfin.pipeline.turn import Turn, run_turn
from yfin.storage.changes import ChangeContext, context_for
from yfin.storage.db import advisory_lock, session_factory
from yfin.storage.variants import ScreenVariantState

log = get_logger(__name__)

MARKET_LOCK_NAME = "yfin_market_sync"
# Symbol field used in the audit record for region-less (global) datasets
GLOBAL_SCOPE_MARKER = "*"


def market_regions(settings: Settings | None = None) -> list[str]:
    """Regions from config, validated against the MarketRegion enum.

    Raised here so an invalid region is `failed` at config time, not `empty`.
    """
    from yfinance import MarketRegion

    cfg = settings or get_settings()
    # MarketRegion isn't a StrEnum: str(member) gives "MarketRegion.US"
    valid = {member.value for member in MarketRegion}
    regions = comma_list(cfg.yf_market_regions, upper=True)
    unknown = [r for r in regions if r not in valid]
    if unknown:
        raise ValueError(f"invalid market region: {', '.join(unknown)}")
    return regions


def default_window(settings: Settings | None = None) -> tuple[date, date]:
    cfg = settings or get_settings()
    today = datetime.now(UTC).date()
    return (
        today - timedelta(days=cfg.yf_calendar_lookback_days),
        today + timedelta(days=cfg.yf_calendar_lookahead_days),
    )


def _run_turn(
    factory: sessionmaker[Any],
    dataset: GlobalDataset[Any],
    mctx: MarketContext,
    scope_label: str,
    tracker: ProxyTracker | None = None,
    changes: ChangeContext | None = None,
) -> list[ItemRecord]:
    """One turn: fetch -> normalize -> upsert, in its own transaction.

    `scope_label` goes into `sync_run_items.symbol`: on this runner a
    "cell" is (dataset x region) or (dataset x screen), not a symbol.
    """
    return run_turn(
        factory,
        Turn(
            dataset=dataset,
            fetch=lambda: dataset.fetch(mctx),
            normalize=dataset.normalize,
            upsert=dataset.upsert,
            audit_key=scope_label,
            registry=MARKET_DATASETS,
            kind=DatasetAxis.MARKET,
            log_context={"scope": scope_label},
            changes=changes,
        ),
        tracker,
    )


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

    factory = session_factory(engine)

    # Market datasets aren't symbol-oriented: queueing and sharding are
    # meaningless here (each dataset is already a single global call).
    # A proxy is still used -- one process, one proxy from the pool.
    # One process, so one shard: `shard_index=0`, and the exporter's
    # sum over shards is a sum of one.
    use_accumulator(Accumulator())
    proxy_id, proxy_label, tracker = setup_single_proxy(factory, cfg, label="market")

    # symbol_count=0 is required: exit_code() only produces code 1 when
    # symbol_count is nonzero; writing the region count would silently
    # shift the "resolved symbol" semantics.
    run_id = open_run(
        factory,
        symbol_count=0,
        dataset_count=len(datasets),
        scope=RunScope.MARKET,
    )

    changes = context_for(
        enabled=cfg.yf_changes_enabled,
        run_id=run_id,
        range_threshold=cfg.yf_changes_range_threshold,
    )

    base_ctx = MarketContext(
        fetched_at=datetime.now(UTC),
        start=window_start,
        end=window_end,
    )

    # Written after every turn, not once at the end. The data of a turn is
    # committed by `run_turn` in its own transaction, so a process that dies
    # mid-run would otherwise leave the data written and no audit row at all
    # for the run, with `sync_runs` stuck in `running`. The symbol runner
    # emits per symbol for the same reason (`runner.py`).
    def emit(records: Sequence[ItemRecord]) -> None:
        write_items(factory, run_id, records, proxy_id=proxy_id, proxy_label=proxy_label)

    for dataset in datasets:
        if dataset.scope == MarketScope.REGION:
            # Region loop is outside the dataset: sync_run_items
            # granularity naturally becomes (dataset x table x region)
            for region in regions:
                emit(
                    _run_turn(
                        factory,
                        dataset,
                        base_ctx.for_region(region),
                        region,
                        tracker,
                        changes,
                    )
                )
        elif dataset.scope == MarketScope.VARIANT:
            # Screen loop is outside for the same reason as the region
            # loop. The variant list is read in its own short-lived
            # session and materialized: turn transactions (`_run_turn`)
            # open their own sessions, so holding a read session open
            # across the loop would waste a connection.
            with factory() as session:
                variants = list(dataset.variants(cfg, ScreenVariantState(session)))
            for variant in variants:
                emit(
                    _run_turn(
                        factory,
                        dataset,
                        base_ctx.for_variant(variant),
                        variant,
                        tracker,
                        changes,
                    )
                )
        else:
            emit(_run_turn(factory, dataset, base_ctx, GLOBAL_SCOPE_MARKER, tracker, changes))

    if tracker is not None:
        with factory() as session:
            tracker.flush(session)
    run_metrics.flush(factory, run_id, shard_index=0)
    return finalize_run(factory, run_id, symbol_count=0, dataset_count=len(datasets))


