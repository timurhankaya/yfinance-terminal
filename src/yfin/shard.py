"""Koordinator: proxy basina bir OS process, dinamik sembol kuyrugu (P4).

Neden process? `yf.config` ve `YfData` yfinance'te PROCESS-GLOBAL
singleton'lardir (config.py:21-61, data.py:83): dort worker thread'i ayni
anda farkli proxy kullanamaz, biri digerinin proxy'sini ezer. Bu yuzden
rotasyonun ekseni thread degil PROCESS'tir.

Yan fayda: `client._bucket` ve `config._settings` de process-global oldugu
icin her shard kendi token-bucket'ini kurar ve rate limit proxy (cikis
IP'si) basina anlam kazanir.
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

from yfin.client import configure_yfinance
from yfin.config import (
    Settings,
    applied_overrides,
    get_settings,
    install_settings,
    settings_from_overrides,
)
from yfin.datasets import SYMBOL_DATASETS
from yfin.db import advisory_lock, create_db_engine
from yfin.logging_setup import configure_logging, get_logger
from yfin.models import Proxy
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
from yfin.runner import (
    RunTally,
    SymbolSource,
    finalize_run,
    list_source,
    open_run,
    record_not_attempted,
    run_shard,
)

if TYPE_CHECKING:
    from multiprocessing.queues import Queue as MPQueue

log = get_logger(__name__)

# Kuyruk sonu isareti. Her shard bir tane tuketir.
SENTINEL = "\x00"

# Child'in kendi havuzu: gercek es zamanli tuketici IKIDIR (WatermarkReader
# kendi kilidiyle okumalari serilestirir, arti ana thread'in sembol
# transaction'i). Invariant: (shard_count x 5) + 2 <= max_connections.
CHILD_POOL_SIZE = 3


class NoEligibleProxy(RuntimeError):
    """--require-proxy verildi ama uygun proxy yok."""


@dataclass(frozen=True)
class ShardSpec:
    """Process sinirindan gecen TUM veri. Pickle'lanabilir olmalidir.

    `__main__` DISINDA bir modulde tanimlidir; spawn ile baslatilan
    child'in hedefi ve argumanlari import edilebilir olmak zorundadir.
    """

    run_id: int
    shard_index: int
    dataset_names: tuple[str, ...]
    full_refresh: bool
    database: str | None
    proxy_id: int | None = None
    proxy_label: str | None = None
    # Cozulmus DSN; parola YALNIZCA bellekte ve bu pipe'ta bulunur,
    # hicbir log satirina girmez (logging_setup.redact_credentials).
    proxy_dsn: str | None = None
    # --start/--end ve sembol evreni secicisi (AH S6.5/7). Bunlar process
    # sinirindan gecmezse proxy havuzu doluyken -- VARSAYILAN yol --
    # child `start=None` ile kosar: "filter" dataset'leri araligi
    # uygulamaz, "none" dataset'leri atlanmaz ve kullanici "aralik
    # uygulandi" sanir. `full_refresh` icin ayni zincir zaten kurulu.
    start: date | None = None
    end: date | None = None
    selector: str | None = None
    # Parent'in COZDUGU DB ezmeleri (CFG S3.5). Child kendi
    # `get_settings()`ini cagirsaydi uc sorun dogardi: (a) araya giren bir
    # `yfin config set` shard-0 ile shard-3'u FARKLI yapilandirmayla
    # kostururdu, (b) N ekstra baglanti acilirdi, (c) child
    # `settings.db_name`e baglanip asil isini `spec.database`de yapar --
    # yani `--database` ile YONLENDIRILMEDIGI semadan ayar okurdu.
    settings_overrides: dict[str, str] = field(default_factory=dict)

    @property
    def proxy_key(self) -> str:
        """tz/cookie/ISIN cache dizini anahtari.

        shard_index ILE ANAHTARLANMAZ: proxy secimi latency/health'e gore
        siralandigi icin shard-0 bir sonraki run'da baska bir proxy
        olabilir ve A'nin IP'siyle mintlenmis cookie B'nin cikis IP'siyle
        kullanilirdi. Cookie cache'in PK'si `strategy`'dir (cache.py:314),
        yani paylasilan bir dosyada tum shard'lar ayni iki satiri ezerdi.
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
    """Child process girisi. MODUL SEVIYESINDE olmak zorundadir (spawn)."""
    # Child DB'ye HIC SELECT atmaz: parent'in cozdugu ezmeler `spec` ile
    # tasindi (CFG S3.5). Kosan bir sync boylece tutarli TEK bir anlik
    # goruntu kullanir.
    settings = settings_from_overrides(spec.settings_overrides)
    install_settings(settings, spec.settings_overrides)

    # ILK ADIM: structlog cache_logger_on_first_use=True ile calisir ve
    # modul seviyesindeki logger'lar ilk kullanimda yapilandirmayi
    # onbellege alir. Bundan once log basilirsa child sessizce
    # yapilandirilmamis PrintLogger'a duser.
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
    """Uygun proxy'ler; bos liste = tek shard, dogrudan baglanti.

    Formul (P4.4):
        shard_count = 1                              --no-proxy ise
                    = max(1, min(N, |eligible|))     aksi halde
    PROXY'SIZ SHARD ASLA ACILMAZ; tek istisna havuzun bos/uygunsuz
    oldugu tek-shard dogrudan baglanti halidir.
    """
    if no_proxy:
        if require_proxy:
            # Ikisi bir arada ANLAMSIZDIR ve sessizce --no-proxy'nin
            # kazanmasi, ban riskini kullaniciya haber vermeden alirdi.
            raise ValueError("--no-proxy ile --require-proxy birlikte verilemez")
        if max_shards is not None and max_shards > 1:
            # Ayni cikis IP'sinden N shard kosmak toplam hizi N katina
            # cikarir; rate limit shard BASINA tanimlidir.
            log.warning("--no-proxy ile shard sayisi 1'e indirildi", requested=max_shards)
        return []

    limit = max_shards if max_shards is not None else settings.yf_max_shards
    eligible = select_eligible(session, max(1, limit))
    if eligible:
        return eligible

    total = count_all(session)
    if require_proxy:
        raise NoEligibleProxy(f"uygun proxy yok (havuzda {total} kayit); --require-proxy verildi")
    if total:
        # Sessizce dogrudan baglanmak ban riskini kullaniciya haber
        # vermeden alirdi.
        log.warning("havuzda proxy var ama hicbiri uygun degil; dogrudan baglaniliyor", total=total)
    else:
        log.info("proxy havuzu bos; dogrudan baglaniliyor")
    return []


def _drain(queue: MPQueue[str]) -> list[str]:
    """Kuyrukta kalan sembolleri toplar (sentinel'ler atilir).

    join()'dan ONCE degil SONRA cagrilir; ama kuyruk her hâlükârda
    bosaltilir, aksi halde feeder thread dolu bir kuyrukta asili kalir.
    """
    leftover: list[str] = []
    while True:
        try:
            value = queue.get_nowait()
        except Empty:
            break
        except (OSError, ValueError):  # pragma: no cover - kapanmis kuyruk
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
        if process.is_alive():  # pragma: no cover - son care
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
    """Advisory lock -> proxy secimi -> run acilisi -> shard'lar -> finalize.

    Advisory lock YALNIZCA burada alinir; child'lar kilit almaz. Kilit,
    child'larin tamami sonlanmadan BIRAKILMAZ: aksi halde parent olunce
    kilit serbest kalir, orphan child'lar yazmaya devam eder ve yeni bir
    cron tetiklemesi ayni satirlara biner (S8.7 garantisi duserdi).
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
            # Endpoint'ler kilit altinda cozulur; Proxy nesneleri
            # session'a bagli, ShardSpec ise saf veridir.
            specs_source = _build_specs(session, eligible, cfg)
            if require_proxy and eligible and not specs_source:
                # SQL uygunluk sorgusu proxy BULDU ama hicbirinin parolasi
                # cozulemedi (tipik neden: YF_PROXY_SECRET_KEY dondu).
                # `_effective_shards`in kontrolu bu noktadan ONCE calisir,
                # bu yuzden burada tekrar bakilir; aksi halde akis
                # "proxy yok" dalina duser ve TUM EVREN operatorun kendi
                # IP'sinden cekilir -- --require-proxy'nin onlemek icin
                # var oldugu senaryonun ta kendisi (cikis kodu da 5 degil
                # 0/2 olurdu).
                raise NoEligibleProxy(
                    f"uygun {len(eligible)} proxy'nin hicbirinin parolasi cozulemedi; "
                    "--require-proxy verildi (YF_PROXY_SECRET_KEY dogru mu?)"
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
            # Tek shard, proxy'siz: ayri process'e gerek yok. yfinance
            # config'i bu process'te de kurulmalidir.
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
            # Proxy'yi dead YAPMAZ: yanlis teshis uretmemek icin acikca
            # raporlanir ve o proxy bu run'da atlanir.
            log.error("proxy parolasi cozulemedi", proxy=row.label, error=str(exc))
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
    # spawn ACIKCA secilir. macOS/3.13'te zaten varsayilandir; acik secim
    # Linux (3.13 varsayilani fork) icin tasinabilirlik geregidir. fork'ta
    # paylasilan soketler ve curl_cffi'nin CFFI handle'lari bozuk davranir.
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
    except ValueError:  # pragma: no cover - ana thread disinda
        previous = None

    try:
        deadline = settings.yf_shard_timeout_seconds
        for process in processes:
            process.join(timeout=deadline)
    except KeyboardInterrupt:
        cancelled = True
        log.warning("iptal edildi; shard'lar sonlandiriliyor")
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)

    crashed = [
        (plan, process)
        for plan, process in zip(plans, processes, strict=True)
        if process.is_alive() or process.exitcode not in (0, None)
    ]
    _terminate(processes)

    # Olen/zaman asimina ugrayan child kendi durumunu flush EDEMEZ; karari
    # parent yazar - ayni saf apply_outcome fonksiyonuyla, politika tek
    # yerde kalir. SHARD_CRASH esikten BAGIMSIZ olarak cooldown uretir.
    if crashed:
        _record_crashes(factory, [plan for plan, _ in crashed], settings)

    leftover = _drain(queue)
    queue.cancel_join_thread()
    queue.close()
    if cancelled:
        log.warning("islenmeyen sembol", count=len(leftover))
    return leftover


def _record_crashes(
    factory: sessionmaker[Session], plans: Sequence[_ProxyPlan], settings: Settings
) -> None:
    policy = ProxyPolicy.from_settings(settings)
    now = datetime.now(UTC)
    with factory() as session:
        for plan in plans:
            log.error("shard beklenmedik sekilde sonlandi", proxy=plan.proxy_label)
            persist_event(
                session,
                plan.proxy_id,
                HealthEvent.SHARD_CRASH,
                policy=policy,
                now=now,
                error="shard sonlandi veya zaman asimina ugradi",
            )
        session.commit()
