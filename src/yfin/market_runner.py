"""Piyasa sync orkestrasyonu (S7.2).

Sembol dongusu yoktur: 6 dataset, tipik ~20 istek. Paralellik, kuyruk ve
backpressure makinesi burada karsiligi olmayan bir karmasiklik olurdu.

Transaction siniri TUR duzeyindedir (dataset x bolge): economic_calendar
patladiginda splits_calendar yazilmis kalir; market_summary'nin EUROPE turu
patladiginda US turu kalir.

Kilit adi 'yfin_market_sync'tir; sembol sync'inin 'yfin_sync' kilidiyle
catismaz, iki komut es zamanli kosabilir.
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
from yfin.persistence import MySQLRowWriter
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
# Bolgesiz (global) dataset'lerin denetim kaydinda kullanilan sembol alani
GLOBAL_SCOPE_MARKER = "*"


def market_regions(settings: Settings | None = None) -> list[str]:
    """Config'teki bolgeler; MarketRegion enum'una gore dogrulanir.

    Gecersiz bolge Market(...) icinde ValueError firlatir -- bu `empty`
    degil `failed`'dir, bu yuzden konfigurasyon asamasinda yakalanir.
    """
    from yfinance import MarketRegion

    cfg = settings or get_settings()
    # MarketRegion bir StrEnum degil: str(member) "MarketRegion.US" verir
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
    """Basarisiz tur icin TABLO BASINA bir denetim kaydi.

    Sembol tarafiyla ayni fonksiyon kullanilir; `_failed_records` registry'yi
    parametre olarak alir, boylece tablo adlari MARKET_DATASETS'ten cozulur.
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
    """Tek tur: fetch -> normalize -> upsert, kendi transaction'inda."""
    started = time.perf_counter()
    try:
        raw = dataset.fetch(mctx)
        result = dataset.normalize(raw)
    except Exception as exc:  # noqa: BLE001 - (dataset x bolge) hata siniri
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
            stats = dataset.upsert(MySQLRowWriter(session), result)
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

    # Piyasa dataset'leri SEMBOL EKSENLI DEGILDIR: kuyruk ve shard'lama
    # burada anlamsizdir (her dataset zaten tek bir global cagridir).
    # Proxy yine de kullanilir - tek process, havuzdan tek proxy.
    proxy_id, proxy_label, tracker = _setup_proxy(factory, cfg)

    # symbol_count=0 ZORUNLUDUR: exit_code() kod 1'i yalnizca symbol_count
    # doluysa uretir; bolge sayisi yazilsaydi "cozulen sembol" semantigi
    # sessizce kayardi
    run_id = open_run(
        factory,
        symbol_count=0,
        dataset_count=len(datasets),
        scope=RunScope.MARKET,
    )

    base_ctx = MarketContext(
        fetched_at=datetime.now(UTC).replace(tzinfo=None),
        start=window_start,
        end=window_end,
    )

    items: list[ItemRecord] = []
    for dataset in datasets:
        if dataset.scope == "region":
            # Bolge dongusu dataset'in DISINDA: sync_run_items granulerligi
            # dogal olarak (dataset x tablo x bolge) olur
            for region in regions:
                items.extend(
                    _run_turn(factory, dataset, base_ctx.for_region(region), region, tracker)
                )
        elif dataset.scope == "variant":
            # SQ S6.1: ekran dongusu, bolge dongusuyle AYNI gerekceyle
            # disaridadir. Varyant listesi KENDI kisa omurlu session'inda
            # okunur ve MADDILESTIRILIR: tur transaction'lari (`_run_turn`)
            # kendi session'larini acar, acik bir okuma session'ini tur
            # boyunca tutmak bosuna bir baglanti tutardi.
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
    """Havuzdan TEK proxy secip yfinance'i ona baglar.

    Uygun proxy yoksa dogrudan baglanti (sembol tarafiyla ayni politika).
    Parolasi cozulemeyen proxy dead YAPILMAZ, atlanir ve raporlanir.
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
