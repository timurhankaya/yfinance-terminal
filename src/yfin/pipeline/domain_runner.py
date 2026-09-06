"""Sector / industry sync orchestration.

Structured like `market_runner.py`, but on a third axis: 156 keys, each its
own HTTP request. `market_runner`'s "6 datasets, one turn" assumption
doesn't hold here, and `SyncContext.ticker` is meaningless for a domain --
hence a separate runner.

No parallelism, no sharding: 156 requests, one process, one proxy. The
symbol side's queue/backpressure machinery would be unneeded complexity
here.

Transaction boundary = turn = (dataset x key x region). A broken industry
doesn't take down the other 144. The one exception is `domain_taxonomy`:
156 `symbols` rows plus 156 `domains` rows are written in a single turn,
single transaction -- the taxonomy is consistent as a whole or not at all.

Lock is `yfin_domain_sync`; doesn't conflict with `yfin_sync` or
`yfin_market_sync`, so all three commands can run concurrently.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import sessionmaker

from yfin.core.config import Settings, get_settings
from yfin.core.errors import classify_error
from yfin.core.logging_setup import get_logger
from yfin.datasets.asof_base import GLOBAL_REGION_MARKER
from yfin.datasets.domain.base import DomainContext, DomainDataset
from yfin.datasets.domain.common import as_of_day, fetch_domain
from yfin.datasets.registry import DOMAIN_DATASETS
from yfin.ingest.client import configure_yfinance
from yfin.models import Domain, DomainType, RunScope
from yfin.pipeline.runner import (
    ItemRecord,
    ProxyTracker,
    RunTally,
    _failed_records,
    _record_items,
    finalize_run,
    open_run,
    write_items,
)
from yfin.proxy import (
    PasswordUndecryptable,
    ProxyPolicy,
    ShardProxyTracker,
    endpoint_of,
    select_eligible,
)
from yfin.storage.db import advisory_lock
from yfin.storage.persistence import PostgresRowWriter

log = get_logger(__name__)

DOMAIN_LOCK_NAME = "yfin_domain_sync"

# Yahoo's fallback region. Always the base: if the primary region were used
# as the base and the primary region itself were invalid
# (YF_DOMAIN_REGIONS=XX), nothing would catch it.
US = "US"

# Measured `topCompanies` overlap with US for supported regions is exactly
# 0 (GB, DE, JP, TR -- all four). In the fallback case overlap is 1.0. The
# 50% threshold leaves ample margin between the two cases and is not
# sensitive to ordering/set drift.
FALLBACK_OVERLAP = 0.5

_REGION_PATTERN = re.compile(r"[A-Z]{2}")

# `sync_run_items.symbol` is NOT NULL; the bootstrap turn has no single
# domain symbol (it writes all 156 at once).
TAXONOMY_SCOPE_MARKER = GLOBAL_REGION_MARKER


class RegionValidationError(ValueError):
    """Configured region is not supported by Yahoo, or malformed."""


def domain_regions(
    settings: Settings | None = None,
    *,
    cache: dict[str, Any] | None = None,
    fetch: Any = None,
) -> list[str]:
    """Format check plus empirical validation.

    Three measured traps:

    1. An invalid region silently returns US (`XX`, `EUROPE`, `''`, `us`) --
       no error. Format checking alone lets `XX` through, and US data would
       get written under the `XX` label.
    2. Using the primary region as the base misses an invalid primary
       region entirely. The base must always be `US`.
    3. List equality doesn't work: `topCompanies` order changed in 8 of 11
       sectors within 15 minutes, and `technology`'s set itself changed.
       If the list drifts between two consecutive requests, an invalid
       region would pass validation -- exactly the scenario the probe
       exists to catch. Set intersection is used instead.

    Cost: 1 request per configured non-US region, plus 1 base request
    unless US is configured. Zero extra requests when only `US` is
    configured, and no guard is needed since US is the fallback itself.
    """
    cfg = settings or get_settings()
    getter = fetch or fetch_domain
    regions = [r.strip().upper() for r in cfg.yf_domain_regions.split(",") if r.strip()]
    if not regions:
        raise RegionValidationError("YF_DOMAIN_REGIONS bos olamaz")
    bad = [r for r in regions if not _REGION_PATTERN.fullmatch(r)]
    if bad:
        raise RegionValidationError(
            f"gecersiz bolge kodu (ISO 3166-1 alpha-2): {', '.join(bad)}"
        )

    candidates = [r for r in regions if r != US]
    if not candidates:
        return regions  # US only: nothing to validate

    ref = cfg.yf_domain_reference_sector
    base_payload = getter(ref, "sector", US)
    if cache is not None:
        # Cache the base request's response so `sector_profile`/
        # `sector_rankings` don't re-fetch it when `US` is configured.
        cache[f"raw:{ref}:{US}"] = base_payload
    base = {c.get("symbol") for c in base_payload.get("topCompanies") or []}
    base.discard(None)

    for region in candidates:
        payload = getter(ref, "sector", region)
        if cache is not None:
            cache[f"raw:{ref}:{region}"] = payload
        probe = {c.get("symbol") for c in payload.get("topCompanies") or []}
        probe.discard(None)
        overlap = len(probe & base) / len(base) if base else 0.0
        if overlap >= FALLBACK_OVERLAP:
            raise RegionValidationError(
                f"bolge {region!r} Yahoo tarafindan desteklenmiyor: referans sektorun "
                f"sirket listesi US ile %{overlap * 100:.0f} ortusuyor "
                f"(esik %{FALLBACK_OVERLAP * 100:.0f}) -- "
                f"US verisi {region!r} etiketiyle yazilacakti"
            )
    return regions


def domain_targets(
    factory: sessionmaker[Any], domain_type: DomainType
) -> list[tuple[str, str]]:
    """(key, symbol) pairs, sourced from the DB.

    Not held in memory, so partial runs like `--datasets industry_profile`
    use the same path. The bootstrap turn runs first on every resolution,
    so the list is always fresh.
    """
    with factory() as session:
        rows = session.execute(
            select(Domain.domain_key, Domain.symbol)
            .where(Domain.domain_type == domain_type)
            .order_by(Domain.domain_key)
        ).all()
    return [(str(key), str(symbol)) for key, symbol in rows]


def domain_parents(factory: sessionmaker[Any]) -> dict[str, str]:
    """Industry key -> parent sector key, from the DB."""
    with factory() as session:
        rows = session.execute(
            select(Domain.domain_key, Domain.parent_key).where(
                Domain.domain_type == DomainType.INDUSTRY
            )
        ).all()
    return {str(key): str(parent) for key, parent in rows if parent is not None}


def _fail(
    dataset: DomainDataset[Any], symbol: str, region: str, exc: Exception
) -> list[ItemRecord]:
    return _failed_records(
        symbol,
        dataset.name,
        f"{type(exc).__name__}: {exc}",
        DOMAIN_DATASETS,
        region=region,
    )


def _run_turn(
    factory: sessionmaker[Any],
    dataset: DomainDataset[Any],
    ctx: DomainContext,
    key: str,
    symbol: str,
    tracker: ProxyTracker | None = None,
) -> list[ItemRecord]:
    """One turn: fetch -> normalize -> upsert, in its own transaction."""
    started = time.perf_counter()
    region = ctx.region
    try:
        raw = dataset.fetch(ctx)
        result = dataset.normalize(raw, key)
    except Exception as exc:  # noqa: BLE001 - boundary is (dataset x key x region)
        kind = classify_error(exc)
        log.warning(
            "domain dataset failed",
            dataset=dataset.name,
            domain_key=key,
            region=region,
            kind=kind.value,
            error=str(exc),
        )
        if tracker is not None:
            tracker.record_error(kind, str(exc))
        return _fail(dataset, symbol, region, exc)
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
            log.error(
                "domain turn failed",
                dataset=dataset.name,
                domain_key=key,
                region=region,
                error=str(exc),
            )
            return _fail(dataset, symbol, region, exc)
    return _record_items(dataset, symbol, stats, fetched, duration, region=region)


def run_domain_sync(
    engine: Engine,
    datasets: Sequence[DomainDataset[Any]],
    *,
    settings: Settings | None = None,
    acquire_lock: bool = True,
) -> RunTally:
    """Order is load-bearing.

    1. `_setup_proxy()` -- one proxy from the pool, `configure_yfinance`
    2. `domain_regions()` -- region validation; after the proxy step
       (otherwise the probe would go out over a direct connection, bypassing
       pool policy), before `open_run` (so bad config doesn't leave a
       `sync_runs` row stuck in `running`)
    3. `open_run(scope=DOMAIN, symbol_count=0)`
    4. `domain_taxonomy` turn
    5. Keys read from the DB
    6. `scope='sector'` datasets, then `scope='industry'`
    7. Per dataset: `regional=False` -> one turn (`region='*'`);
       `regional=True` -> one turn per region
    """
    cfg = settings or get_settings()
    if acquire_lock:
        with advisory_lock(engine, DOMAIN_LOCK_NAME):
            return run_domain_sync(engine, datasets, settings=cfg, acquire_lock=False)

    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    proxy_id, proxy_label, tracker = _setup_proxy(factory, cfg)

    cache: dict[str, Any] = {}
    regions = domain_regions(cfg, cache=cache)
    primary = regions[0]

    # symbol_count=0 is required: `exit_code()` only produces code 1 when
    # symbol_count is set. Writing the key count instead would silently
    # shift "resolved symbol" semantics (same reasoning as market_runner).
    run_id = open_run(
        factory,
        symbol_count=0,
        dataset_count=len(datasets),
        scope=RunScope.DOMAIN,
        selector=f"regions={','.join(regions)}",
    )

    fetched_at = datetime.now(UTC)
    base_ctx = DomainContext(
        fetched_at=fetched_at,
        as_of_date=as_of_day(fetched_at),
        primary_region=primary,
    )
    base_ctx._cache.update(cache)

    items: list[ItemRecord] = []
    selected = list(datasets)

    # 4. Bootstrap: one turn, one transaction.
    for dataset in [d for d in selected if not d.per_key]:
        items.extend(
            _run_turn(factory, dataset, base_ctx, TAXONOMY_SCOPE_MARKER,
                      TAXONOMY_SCOPE_MARKER, tracker)
        )

    # 5. Keys from the DB. The bootstrap turn runs first on every
    #    resolution, so the list is always fresh.
    base_ctx.parents.update(domain_parents(factory))
    targets = {
        "sector": domain_targets(factory, DomainType.SECTOR),
        "industry": domain_targets(factory, DomainType.INDUSTRY),
    }

    # 6. Sectors first, then industries.
    per_key = [d for d in selected if d.per_key]
    ordered = [d for d in per_key if d.scope == "sector"] + [
        d for d in per_key if d.scope == "industry"
    ]
    for dataset in ordered:
        turn_regions = regions if dataset.regional else [GLOBAL_REGION_MARKER]
        for key, symbol in targets[dataset.scope]:
            target_ctx = base_ctx.for_target(key, dataset.scope)
            for region in turn_regions:
                items.extend(
                    _run_turn(
                        factory, dataset, target_ctx.for_region(region), key, symbol, tracker
                    )
                )

    write_items(factory, run_id, items, proxy_id=proxy_id, proxy_label=proxy_label)
    if tracker is not None:
        with factory() as session:
            tracker.flush(session)
    return finalize_run(factory, run_id, symbol_count=0, dataset_count=len(datasets))


def _setup_proxy(
    factory: sessionmaker[Any], settings: Settings
) -> tuple[int | None, str | None, ProxyTracker | None]:
    """Picks one proxy from the pool and points yfinance at it.

    Same policy as `market_runner._setup_proxy`: falls back to a direct
    connection if no proxy is eligible; a proxy whose password can't be
    decrypted is skipped, not marked dead.
    """
    with factory() as session:
        for row in select_eligible(session, limit=1):
            try:
                endpoint = endpoint_of(row, settings)
            except PasswordUndecryptable as exc:
                log.error("proxy parolasi cozulemedi", proxy=row.label, error=str(exc))
                continue
            configure_yfinance(endpoint.dsn(), proxy_key=f"proxy-{row.id}", settings=settings)
            log.info("domain sync proxy", proxy=row.label)
            return (
                int(row.id),
                row.label,
                ShardProxyTracker(int(row.id), ProxyPolicy.from_settings(settings)),
            )
    configure_yfinance(None, proxy_key="direct", settings=settings)
    log.info("domain sync dogrudan baglaniliyor")
    return None, None, None
