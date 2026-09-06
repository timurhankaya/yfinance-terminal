"""Sektor / endustri sync orkestrasyonu (SI S6.7).

`market_runner.py`'nin yapisi, ama UCUNCU bir eksende: 156 anahtar, her biri
kendi HTTP istegi. `market_runner`'in "6 dataset, tek tur" varsayimi burada
tutmaz ve `SyncContext.ticker` domain icin anlamsizdir -- bu yuzden ayri bir
runner.

PARALELLIK / SHARD YOKTUR. 156 istek, tek process, tek proxy. Sembol
tarafindaki kuyruk/backpressure makinesi burada karsiligi olmayan bir
karmasiklik olurdu (`market_runner`'in gerekcesi).

TRANSACTION SINIRI = TUR = (dataset x anahtar x bolge). Bozuk bir endustri
diger 144'u dusurmez. TEK ISTISNA `domain_taxonomy`: 156 `symbols` + 156
`domains` satiri tek turda, tek transaction'da yazilir -- taksonomi ya butun
olarak tutarlidir ya hic.

Kilit `yfin_domain_sync`; `yfin_sync` ve `yfin_market_sync` ile catismaz, uc
komut es zamanli kosabilir.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import sessionmaker

from yfin.client import configure_yfinance
from yfin.config import Settings, get_settings
from yfin.datasets.asof_base import GLOBAL_REGION_MARKER
from yfin.datasets.domain.base import DomainContext, DomainDataset
from yfin.datasets.domain.common import as_of_day, fetch_domain
from yfin.datasets.registry import DOMAIN_DATASETS
from yfin.db import advisory_lock
from yfin.errors import classify_error
from yfin.logging_setup import get_logger
from yfin.models import Domain, DomainType, RunScope
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

DOMAIN_LOCK_NAME = "yfin_domain_sync"

# Yahoo'nun geri dusus bolgesi. Taban HER ZAMAN budur: birincil bolge taban
# alinsaydi ve birincil bolgenin KENDISI gecersiz olsaydi
# (YF_DOMAIN_REGIONS=XX) hicbir sey yakalanmazdi.
US = "US"

# Desteklenen bolgelerin US ile `topCompanies` kesisimi TAM OLARAK 0
# olculdu (GB, DE, JP, TR -- dordunde de). Geri dusus durumunda kesisim
# 1,0'dir. %50 esigi iki durumu ayirmak icin fazlasiyla genis pay birakir ve
# sira/kume kaymasindan etkilenmez.
FALLBACK_OVERLAP = 0.5

_REGION_PATTERN = re.compile(r"[A-Z]{2}")

# `sync_run_items.symbol` NOT NULL; bootstrap turunun tek bir domain
# sembolu yoktur (156'sini birden yazar).
TAXONOMY_SCOPE_MARKER = GLOBAL_REGION_MARKER


class RegionValidationError(ValueError):
    """Yapilandirilmis bolge Yahoo tarafindan desteklenmiyor ya da bicimsiz."""


def domain_regions(
    settings: Settings | None = None,
    *,
    cache: dict[str, Any] | None = None,
    fetch: Any = None,
) -> list[str]:
    """Bicim + AMPIRIK dogrulama (SI S6.6).

    UC TUZAK VAR VE UCU DE OLCULDU:

    1. Gecersiz bolge SESSIZCE US donduruyor (`XX`, `EUROPE`, `''`, `us`) --
       hata yok. Bicim kontrolu tek basina `XX`i gecirir ve US verisi `XX`
       etiketiyle yazilirdi.
    2. Taban olarak BIRINCIL bolge alinirsa, birincil bolgenin kendisi
       gecersizse hicbir sey yakalanmaz. Taban HER ZAMAN `US` olmalidir.
    3. LISTE ESITLIGI KULLANILAMAZ: `topCompanies` sirasi 15 dakikada 11
       sektorun 8'inde, `technology`de KUMESI BILE degisti. Iki ardisik
       istek arasinda liste kayarsa gecersiz bir bolge dogrulamayi GECER --
       tam olarak probun engellemek icin var oldugu senaryo. Bu yuzden
       KUME KESISIMI kullanilir.

    Maliyet: US-disi yapilandirilmis bolge basina 1 istek, arti US
    yapilandirilmamissa 1 taban istegi. `US` tek basinayken SIFIR ek istek
    -- ve koruma da gerekmez, cunku US geri dususun kendisidir.
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
        return regions  # yalniz US: dogrulanacak bir sey yok

    ref = cfg.yf_domain_reference_sector
    base_payload = getter(ref, "sector", US)
    if cache is not None:
        # Taban istegin yaniti onbellege konur; `US` yapilandirilmissa
        # `sector_profile`/`sector_rankings` onu YENIDEN CEKMEZ.
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
    """(anahtar, sembol) ciftleri; kaynak DB'dir.

    Bellekte tasinmamasi, `--datasets industry_profile` gibi KISMI
    kosularda da ayni yolu kullanmasini saglar (SI S6.5). Bootstrap her
    cozumlemede basa eklendigi icin liste her zaman tazedir.
    """
    with factory() as session:
        rows = session.execute(
            select(Domain.domain_key, Domain.symbol)
            .where(Domain.domain_type == domain_type)
            .order_by(Domain.domain_key)
        ).all()
    return [(str(key), str(symbol)) for key, symbol in rows]


def domain_parents(factory: sessionmaker[Any]) -> dict[str, str]:
    """Endustri anahtari -> DB'deki ebeveyn sektor anahtari (SI S7.3)."""
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
    """Tek tur: fetch -> normalize -> upsert, KENDI transaction'inda."""
    started = time.perf_counter()
    region = ctx.region
    try:
        raw = dataset.fetch(ctx)
        result = dataset.normalize(raw, key)
    except Exception as exc:  # noqa: BLE001 - (dataset x anahtar x bolge) siniri
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
    """SIRA BAGLAYICIDIR (SI S6.7).

    1. `_setup_proxy()` -- havuzdan tek proxy, `configure_yfinance`
    2. `domain_regions()` -- bolge dogrulamasi; proxy'den SONRA (aksi halde
       prob dogrudan baglantidan giderdi ve havuz politikasi disinda
       kalirdi), `open_run`dan ONCE (hatali yapilandirma `running`
       durumunda bir `sync_runs` satiri birakmasin)
    3. `open_run(scope=DOMAIN, symbol_count=0)`
    4. `domain_taxonomy` turu
    5. Anahtarlar DB'den okunur
    6. `scope='sector'` dataset'leri, sonra `scope='industry'`
    7. Her dataset icin: `regional=False` -> tek tur (`region='*'`);
       `regional=True` -> bolge basina bir tur
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

    # symbol_count=0 ZORUNLUDUR: `exit_code()` kod 1'i yalnizca
    # symbol_count doluysa uretir; anahtar sayisi yazilsaydi "cozulen
    # sembol" semantigi sessizce kayardi (market_runner ile ayni gerekce).
    run_id = open_run(
        factory,
        symbol_count=0,
        dataset_count=len(datasets),
        scope=RunScope.DOMAIN,
        selector=f"regions={','.join(regions)}",
    )

    fetched_at = datetime.now(UTC).replace(tzinfo=None)
    base_ctx = DomainContext(
        fetched_at=fetched_at,
        as_of_date=as_of_day(fetched_at),
        primary_region=primary,
    )
    base_ctx._cache.update(cache)

    items: list[ItemRecord] = []
    selected = list(datasets)

    # 4. Bootstrap: TEK tur, TEK transaction.
    for dataset in [d for d in selected if not d.per_key]:
        items.extend(
            _run_turn(factory, dataset, base_ctx, TAXONOMY_SCOPE_MARKER,
                      TAXONOMY_SCOPE_MARKER, tracker)
        )

    # 5. Anahtarlar DB'den. Bootstrap her cozumlemede basa eklendigi icin
    #    liste her zaman tazedir.
    base_ctx.parents.update(domain_parents(factory))
    targets = {
        "sector": domain_targets(factory, DomainType.SECTOR),
        "industry": domain_targets(factory, DomainType.INDUSTRY),
    }

    # 6. Once sektor, sonra endustri.
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
    """Havuzdan TEK proxy secip yfinance'i ona baglar.

    `market_runner._setup_proxy` ile AYNI politika: uygun proxy yoksa
    dogrudan baglanti; parolasi cozulemeyen proxy dead YAPILMAZ, atlanir.
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
