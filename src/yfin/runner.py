"""Orkestrasyon: paralellik, kuyruk, transaction siniri, hata izolasyonu (S7, S8)."""

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

from yfin.client import make_ticker
from yfin.config import Settings, get_settings
from yfin.datasets.base import Dataset, NormalizedResult, SyncContext, WriteStats
from yfin.datasets.meta import DatasetMeta
from yfin.datasets.registry import SYMBOL_DATASETS, Registry
from yfin.db import advisory_lock
from yfin.errors import PROXY_FAULT_KINDS, DatasetOutOfScope, ErrorKind, classify_error
from yfin.logging_setup import bind_shard_context, get_logger
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
from yfin.persistence import PostgresRowWriter
from yfin.rescale import apply_pending

log = get_logger(__name__)

# S8.1 cikis kodlari
EXIT_OK = 0
EXIT_NO_SYMBOL_RESOLVED = 1
EXIT_PARTIAL = 2
EXIT_ALL_FAILED = 3
EXIT_LOCK_NOT_ACQUIRED = 4
EXIT_NO_PROXY = 5  # --require-proxy verildi, uygun proxy yok (P8.1)

# --start/--end verildiginde date_range="none" dataset'inin atlanma gerekcesi
SKIP_DATE_RANGE = "date_range=none"

# intraday_scope kapsami disindaki hucrenin gerekcesi (PB S6.5)
SKIP_OUT_OF_SCOPE = "intraday_scope kapsami disi"


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
    # Domain (sektor / endustri) hucrelerinin bolge ekseni (SI S5.9/8.1).
    # Sembol ve piyasa tarafinda NULL kalir: `market_runner` bolgeyi
    # `symbol` alanina yaziyor ve o davranis BILINCLI olarak
    # degistirilmedi (SI S5.9) -- degistirmek mevcut denetim sorgularini
    # kirardi.
    region: str | None = None


@dataclass
class SymbolPayload:
    """Worker ciktisi: bir sembolun tum normalize edilmis sonuclari."""

    symbol: str
    resolved: bool
    results: list[tuple[Dataset[Any], NormalizedResult, int, int]] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)
    # Calistirilmadan elenen dataset'ler (ad, gerekce) - AH S6.5/3.
    # Ucuncu bir kanal gerekir: bunlar ne sonuc ne hatadir; kayitsiz
    # atlanirlarsa --start verilen bir run'da 14 dataset denetimden kaybolur.
    skipped: list[tuple[str, str]] = field(default_factory=list)
    # Kapsam disi birakilan dataset'ler (ad, gerekce) - PB S6.5.
    # `skipped`ten AYRI tutulur cunku farkli bir ItemStatus'e gider:
    # skipped "content_hash/date_range nedeniyle elendi", out_of_scope
    # ise "bu sembol bu interval icin hic hedeflenmedi" demektir.
    out_of_scope: list[tuple[str, str]] = field(default_factory=list)
    error: str | None = None
    # Proxy saglik muhasebesi icin (P5.2). Tuketici thread'de islenir,
    # boylece tracker uzerinde kilide gerek kalmaz.
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
        """failed yok -> 0 (ok U empty U skipped normaldir) - S8.1."""
        if self.symbol_count and self.resolved_symbols == 0:
            return EXIT_NO_SYMBOL_RESOLVED
        if self.failed == 0:
            return EXIT_OK
        # RunTally.cells ile AYNI dislama kumesi: hic denenmemis hucreler
        # ("denenen her sey basarisiz" hesabina) girmemelidir.
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
    """Salt-okunur watermark saglayici (S7.3). Kendi kisa oturumunu acar,
    boylece worker thread'leri ana transaction'a dokunmaz."""

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
        # price_bars interval basina ayri watermark ister (PB S6.3).
        for name, value in (where or {}).items():
            conditions.append(target.c[name] == value)
        stmt = select(func.max(target.c[column])).where(and_(*conditions))
        with self._lock, self._factory() as session:
            result = session.execute(stmt).scalar_one_or_none()
        return result


class ScopeReader:
    """intraday_scope cozumleyicisi (PB S6.5a).

    SyncContext'in DB erisimi YOKTUR ve `_worker` her SEMBOL icin yeni bir
    SyncContext kurar; kapsam sorgusu ctx.cached'e birakilsaydi kosu
    basina ~5.000 sorgu olurdu. Kume BU ORNEKTE, kosu basina BIR KEZ
    okunur. Shard child process'lerinin her biri kendi ornegini alir.
    """

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory
        self._lock = threading.Lock()
        self._cache: dict[str, frozenset[str] | None] = {}

    def _symbols_for(self, interval: str) -> frozenset[str] | None:
        """O interval icin kapsam kumesi; None = TUM EVREN.

        Kural bar_interval BAZINDA ve `enabled` degerinden BAGIMSIZ
        uygulanir: "o interval icin en az bir satir var mi?". Yalniz
        enabled=0 satirlari olan bir interval de "kayit var" sayilir ve
        hicbir sembol kosar (PB S5.4).
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
                # 1m'de kayit yoksa HICBIR sembol (1,21 milyar satir/yil
                # riski); digerlerinde tum evren (PB S5.4 asimetrisi).
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
    """Acik (cozulmemis) bosluklari okur (PB S6.2/5).

    Bu geri besleme olmadan bar_gaps yalnizca bir mezar tasi olurdu:
    ortadaki bir dilim dusup sonrakiler yazildiginda watermark boslugun
    OTESINE gecer ve o pencere bir daha hic istenmezdi.
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
    """RESOLVE adimi: fast_info + history_metadata -> symbols satiri.

    Cozulemezse (None, hata) doner ve sembolun tum dataset'leri atlanir
    (S7.1). normalize da korunur: FastInfo tembeldir ve gecersiz sembolde
    anahtar erisiminde KeyError firlatir.
    """
    try:
        raw = bootstrap.fetch(ctx)
        result = bootstrap.normalize(raw, ctx.symbol)
    except Exception as exc:  # noqa: BLE001 - hata sinirinin ta kendisi
        kind = classify_error(exc)
        log.warning("symbol resolve failed", symbol=ctx.symbol, kind=kind.value, error=str(exc))
        return None, f"{type(exc).__name__}: {exc}", kind
    if result.is_empty:
        # Bos sonuc bir AG hatasi degildir; proxy cezalandirilmaz.
        return None, "sembol cozulemedi: bos sonuc", None
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
    """fetch + normalize (ag ve saf donusum). DB yazimi ana thread'dedir."""
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
        # AH S6.5/4: aralik verildiginde 'none' dataset'i FETCH'TEN ONCE
        # elenir. Sessizce calistirmak kullaniciya "aralik uygulandi" sanisi
        # verirdi; kayitsiz atlamak denetimi delerdi.
        if ranged and dataset.date_range == "none":
            payload.skipped.append((dataset.name, SKIP_DATE_RANGE))
            continue
        started = time.perf_counter()
        try:
            raw = dataset.fetch(ctx)
            result = dataset.normalize(raw, symbol)
        except DatasetOutOfScope as exc:
            # JENERIK except'ten ONCE gelmek ZORUNDA (PB S6.5b): asagiya
            # duserse classify_error'a ugrar, FAILED yazilir ve kapsam
            # disi bir sembol proxy saglik muhasebesini kirletir.
            payload.out_of_scope.append((dataset.name, str(exc)))
            continue
        except Exception as exc:  # noqa: BLE001 - (sembol x dataset) hata siniri
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
    """Cok tabloya yazan dataset'ler icin TABLO BASINA bir satir (S8.1)."""
    records: list[ItemRecord] = []
    # `or [None]`: produces=() olan izleme dataset'i (sustainability) aksi
    # halde HIC satir yazmaz ve denetimden kaybolur (AH S6.5/1).
    tables: list[str | None] = list(stats.tables() or dataset.produces) or [None]
    for position, table in enumerate(tables):
        # table is None YALNIZCA izleme dataset'inde olur (hicbir tabloya
        # yazmaz); o durumda tum sayaclar 0'dir ve hucre EMPTY olur.
        attempted = stats.attempted.get(table, 0) if table else 0
        verified = stats.verified.get(table, 0) if table else 0
        skipped = stats.skipped.get(table, 0) if table else 0
        if attempted == 0 and skipped == 0:
            status = ItemStatus.EMPTY  # kaynak veri yok - HATA DEGIL (S8.2)
        elif attempted == 0 and skipped:
            status = ItemStatus.SKIPPED  # content_hash degismedi
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
                # `fetched` dataset'in tamamindan cekilen satir sayisidir,
                # tablo basina degil. Her tablo satirina yazilsaydi
                # sync_runs.rows_fetched cok tabloya yazan dataset'lerde
                # (info: 3 tablo, news: 2) katlanarak sisirdi. Bu yuzden
                # yalnizca ilk tablo satirina yazilir.
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
    """Basarisiz hucre icin TABLO BASINA bir kayit (S8.1).

    Tek bir table_name=NULL satiri yazilsaydi denetim sorgulari tablo
    bazinda filtrelenemez, "bu tablo en son ne zaman basarisiz oldu"
    sorusu yanitlanamazdi.
    """
    dataset = registry.get(dataset_name)
    # Ayni bosluk HATA yolunda da vardi: produces=() -> tuple(()) -> hic
    # satir. `or (None,)` bunu kapatir (AH S6.5/2).
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
    """Calistirilmadan elenen hucre icin TABLO BASINA bir kayit (AH S6.5/5).

    `_failed_records` ile ayni tablo-basina-satir kurali; tek fark durum ve
    gerekcenin `error` alaninda tasinmasi. `status` OUT_OF_SCOPE icin de
    kullanilir (PB S6.5c) - iki ayri fonksiyon ayni govdeyi tekrarlardi.
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
    """Sembol basina TEK transaction (S8.7): ya butun olarak yazilir ya hic."""
    records: list[ItemRecord] = []
    writer = PostgresRowWriter(session)
    # Rescale kancasi (PB S6.6): price_bars YAZILMADAN ONCE ve AYNI
    # transaction icinde. Ters sirada, bu kosuda yazilan yeni barlar
    # (zaten Yahoo'nun guncel olceginde) bir kez daha bolunurdu.
    # Transaction'i ayirmak da olmazdi: _persist_with_retry kilit
    # cakismasinda tum blogu yeniden calistirir ve commit edilmis bir
    # rescale ikinci kez uygulanmasa bile muhasebeyi bulanistirirdi.
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
    """price_bars yazan bir dataset varsa bekleyen split'leri uygular.

    Yalniz price_bars yazilacaksa calisir: `--datasets info` gibi bir kosu
    bos yere splits/bar_rescales sorgusu yapmamalidir.
    """
    writes_bars = any(
        "price_bars" in dataset.produces for dataset, _result, _f, _d in payload.results
    )
    if not writes_bars:
        return
    try:
        apply_pending(session, payload.symbol)
    except Exception as exc:  # noqa: BLE001 - kancanin hatasi sembolu dusurmemeli
        # Ayni transaction'da oldugumuz icin burada YUTMAK tehlikelidir:
        # bozulmus bir olcekleme sessizce kalirdi. Bu yuzden yeniden
        # firlatilir; _persist_with_retry ve cagiran katman ilgilenir.
        log.error("rescale hook failed", symbol=payload.symbol, error=str(exc))
        raise


def _mark_unknown(session: Session, symbol: str, threshold: int) -> None:
    """S8.8: ardisik 5 calistirmada unknown_symbol alan sembol is_active=0.
    Veri SILINMEZ."""
    row = session.get(Symbol, symbol)
    if row is None:
        # Evren elle yonetilir; kayitli olmayan sembol icin sayac tutulmaz.
        # Sessiz kalmak yerine gorunur kilinir.
        log.info("unknown symbol not tracked (not in symbols table)", symbol=symbol)
        return
    streak = (row.unknown_streak or 0) + 1
    session.execute(
        update(Symbol)
        .where(Symbol.symbol == symbol)
        .values(unknown_streak=streak, is_active=streak < threshold)
    )


# --------------------------------------------------------------------------
# Sembol kaynagi
# --------------------------------------------------------------------------

# Bir shard'in sirada ne isleyecegini soran cagri. None = kaynak tukendi.
# Tek shard'da liste ustunde, cok shard'da mp.Queue ustunde calisir; runner
# ikisini ayirt etmez.
SymbolSource = Callable[[], str | None]


def list_source(symbols: Sequence[str]) -> SymbolSource:
    """Liste tabanli kaynak; thread'ler arasinda paylasilabilir."""
    iterator = iter(symbols)
    lock = threading.Lock()

    def _next() -> str | None:
        with lock:
            return next(iterator, None)

    return _next


class ProxyTracker(Protocol):
    """runner'in proxy saglik muhasebesinden gordugu TEK arayuz.

    Somut uygulama `yfin.proxy.ShardProxyTracker`'dir; runner ona degil bu
    soyutlamaya baglidir, boylece proxy politikasi runner'i degistirmeden
    evrilebilir ve testler sahte bir tracker verebilir.
    """

    withdrawn: bool

    def record_success(self) -> None: ...

    def record_error(self, kind: ErrorKind, message: str | None = None) -> None: ...

    def flush(self, session: Session) -> None: ...


@dataclass
class ShardCounters:
    """Parent'a giden ILERLEME telemetrisi. YETKILI DEGILDIR (P4.5).

    Run toplamlari ve cikis kodu DB'den (`sync_run_items`) hesaplanir;
    aksi halde iki ayri kaynak birbirini tutmayabilirdi.
    """

    shard_index: int = 0
    symbols_seen: int = 0
    symbols_unresolved: int = 0
    cells_failed: int = 0
    withdrawn: bool = False


# --------------------------------------------------------------------------
# Run yasam dongusu
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
    """sync_runs satirini acar ve COMMIT EDER.

    Commit sarttir: child'lar ayri bir baglanti kullanir ve commit
    edilmemis bir run_id'ye item yazmak ERROR 1452 verirdi.
    """
    with factory() as session:
        run = SyncRun(
            started_at=datetime.now(UTC),
            scope=scope,
            status=RunStatus.RUNNING,
            symbol_count=symbol_count,
            dataset_count=dataset_count,
            shard_count=shard_count,
            # Hangi run'in hangi evreni kapsadigi aksi halde geriye donuk
            # bilinemez ve "eksiksizlik" iddiasi denetlenemez (AH S5.6).
            selector=selector,
        )
        session.add(run)
        session.commit()
        return int(run.id)


# PostgreSQL SQLSTATE'leri. Semboller shard'lara dagitildigi icin iki
# process ayni news / news_symbols satirina yazabilir; tek process'te bu
# risk yoktu.
#   40001 serialization_failure
#   40P01 deadlock_detected
#
# 55P03 (lock_not_available) LISTEDE YOKTUR: bu kod yolunda hic olusmaz
# cunku NOWAIT / SKIP LOCKED kullanilmiyor. Gerekcesiz bir SQLSTATE'i
# yeniden denemek, ileride NOWAIT eklenirse yanlis davranisi sessizce
# mesrulastirirdi.
_RETRYABLE_SQLSTATES = frozenset({"40001", "40P01"})


def _is_lock_conflict(exc: BaseException) -> bool:
    """Hata METNI degil SQLSTATE'e bakilir.

    Metin eslesmesi yerellestirilmis mesajlardan ve surucu bicim
    degisikliklerinden etkilenir; SQLSTATE yapisal ve sabittir.
    psycopg3 istisnalari `sqlstate` tasir ve SQLAlchemy onu
    `DBAPIError.orig` altinda sunar. `orig` tasimayan bir istisnada
    (programlama hatasi) getattr zinciri None doner ve YENIDEN DENENMEZ --
    dogru davranis.
    """
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    return sqlstate in _RETRYABLE_SQLSTATES


def _persist_with_retry(
    factory: sessionmaker[Session], payload: SymbolPayload, attempts: int
) -> list[ItemRecord]:
    """Sembol transaction'i; kilit catismasinda jitter'li yeniden deneme.

    Transaction sembol kapsamli (S8.7) ve idempotent (S7.2) oldugu icin
    yeniden calistirmak guvenlidir. PostgreSQL'de hata alan transaction
    HER ZAMAN abort durumuna gecer ve ROLLBACK disinda komut kabul etmez,
    bu yuzden yeniden denemeden once rollback ZORUNLUDUR -- motorun
    kendisi bunu dayatir.
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
    # Diger UC kanal da denetimde kalir. Bunlar yazma katmanina BAGLI
    # DEGILDIR: `failures` fetch sirasinda patlamistir, `skipped` ve
    # `out_of_scope` ise hic aga cikmamistir. Birakilsalardi o hucreler
    # icin HICBIR satir olusmazdi -- `failed` bile degil, YOKLUK; ve
    # "bu dataset en son ne zaman denendi" sorgusu sessizce yaniltirdi.
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
    """Denetim kayitlarini AYRI bir transaction'da yazar.

    Sembol transaction'i ile ayni transaction'da olsaydi rollback denetim
    izini de silerdi - oysa basarisizligin kaydi tam da o durumda gerekir.
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
    """Tek bir shard: kuyruktan sembol ceker, isler, denetim kaydini yazar.

    Paralellik ekseni SEMBOL'dur (S7.1); shard yalnizca bir seviye disariya
    eklenen process sinifidir.
    """
    cfg = settings or get_settings()
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    watermarks = WatermarkReader(factory)
    # Kapsam kumesi kosu basina BIR KEZ okunur (PB S6.5a); acik bosluklar
    # sembol basina sorgulanir cunku sembole ozguddur.
    scopes = ScopeReader(factory)
    gaps = GapReader(factory)
    bootstrap = next(d for d in datasets if d.name == SYMBOL_DATASETS.bootstrap)
    counters = ShardCounters(shard_index=shard_index)

    # Kuyruk maxsize ile SINIRLIDIR (S7.1). Sinirin gercekten baglayici
    # olmasi icin sonucu KUYRUGA WORKER'IN KENDISI koyar: kuyruk dolunca
    # worker put() uzerinde bloke olur ve yeni sembol cekilmez.
    results: queue.Queue[SymbolPayload | None] = queue.Queue(maxsize=cfg.yf_queue_maxsize)

    def worker_loop() -> None:
        # contextvars ThreadPoolExecutor worker'larina KOPYALANMAZ; bagl.
        # her thread'in basinda yeniden yapilir (P6.5).
        bind_shard_context(run_id, shard_index, proxy_label)
        while True:
            # Proxy cooldown'a girdiyse shard kendini geri ceker (P4.7).
            # Aksi halde banlanmis proxy 429'u aninda aldigi icin kuyruktan
            # EN COK sembolu ceker ve hepsini failed isaretlerdi.
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
                    # fetched_at DB fonksiyonuyla degil, Python tarafinda
                    # SEMBOL BASINA BIR KEZ uretilir (S5.4)
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
            results.put(payload)  # kuyruk doluysa burada bloke olur

    def produce() -> None:
        try:
            with ThreadPoolExecutor(max_workers=cfg.yf_max_workers) as pool:
                futures = [pool.submit(worker_loop) for _ in range(cfg.yf_max_workers)]
                for future in futures:
                    future.result()
        except BaseException as exc:  # noqa: BLE001
            log.error("producer crashed", error=f"{type(exc).__name__}: {exc}")
        finally:
            # Sentinel MUTLAKA yazilmalidir: aksi halde executor beklenmedik
            # bir hata verdiginde tuketici results.get() uzerinde suresiz
            # bloke olur (kuyrukta timeout yoktur).
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

        # Proxy saglik muhasebesi TEK THREAD'de yapilir; tracker uzerinde
        # kilide gerek kalmaz.
        if tracker is not None:
            for kind in payload.error_kinds:
                tracker.record_error(kind)
            if payload.success_count:
                tracker.record_success()
            if tracker.withdrawn:
                with factory() as session:
                    tracker.flush(session)

        if not payload.resolved:
            # TASIMA hatasi sembolun sucu DEGILDIR (P5.2'nin simetrigi).
            # Olu bir proxy'de her sembol cozulemez; bunlar unknown_symbol
            # sayilsaydi delist sayaci dolar ve yf_delist_threshold kosu
            # sonunda TUM EVREN sessizce is_active=0 olurdu.
            transport_fault = any(k in PROXY_FAULT_KINDS for k in payload.error_kinds)
            status = ItemStatus.FAILED if transport_fault else ItemStatus.UNKNOWN_SYMBOL
            emit(
                [
                    ItemRecord(
                        symbol=payload.symbol,
                        dataset=SYMBOL_DATASETS.bootstrap or "symbols",
                        status=status,
                        error=payload.error or "sembol cozulemedi",
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
    """Kuyrukta islenmeden kalan semboller (P4.7).

    Bu satirlar yazilmasaydi o semboller icin sync_run_items'ta HIC kayit
    olmaz, failed sayisi sifir kalir ve evrenin yarisi hic cekilmemisken
    run 'ok' + exit 0 donerdi.
    """
    write_items(
        factory,
        run_id,
        [
            ItemRecord(
                symbol=symbol,
                dataset=SYMBOL_DATASETS.bootstrap or "symbols",
                status=ItemStatus.NOT_ATTEMPTED,
                error="shard cekildi; sembol kuyrukta kaldi",
            )
            for symbol in symbols
        ],
    )


@dataclass
class RunTally:
    """Run sonucunun TEK dogruluk kaynagi: sync_run_items agregasyonu."""

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
        """unknown_symbol, not_attempted ve out_of_scope disi hucreler.

        out_of_scope da DISLANIR: "denenen her sey basarisiz" hesabina
        hic denenmemis hucreler girmemelidir. 4.500 kapsam disi hucre
        sayilsaydi, gercekten denenen 500 hucrenin tamami dusse bile
        EXIT_ALL_FAILED yerine EXIT_PARTIAL donerdi.
        """
        excluded = {
            ItemStatus.UNKNOWN_SYMBOL.value,
            ItemStatus.NOT_ATTEMPTED.value,
            ItemStatus.OUT_OF_SCOPE.value,
        }
        return sum(v for k, v in self.counts.items() if k not in excluded)

    def exit_code(self) -> int:
        """failed yok -> 0 (ok U empty U skipped normaldir) - S8.1.

        ISLENMEMIS SEMBOL VARKEN CIKIS KODU ASLA 0 OLAMAZ (P8.1).
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
    """Toplamlari ve cikis kodunu DB'den hesaplar, sync_runs'i kapatir.

    Parent'a sonuc kuyruguyla ozet TASINMAZ: sync_runs toplamlari kuyruktan,
    sync_run_items gercegi child'lardan gelseydi ikisi birbirini tutmayabilir
    ve S8.6'nin "makine tarafindan dogrulanabilir eksiksizlik" iddiasi
    zayiflardi.

    CAGIRAN, tum child'lari join ETMIS olmalidir; aksi halde agregasyon
    yarim veri uzerinde kosar.
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

        # Cozulmus sembol = hic unknown_symbol satiri OLMAYAN sembol.
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

        # MUTABAKAT (P8.1): denetimde HIC satiri olmayan sembol, coken ya da
        # timeout'a dusen bir shard'in kuyruktan alip bitiremedigi semboldur.
        # `shard.py` yalnizca kuyrukta KALANI drain eder; child'in elindeki
        # sembol icin hicbir `sync_run_items` satiri olusmaz. Bu fark
        # sayilmasaydi agregasyon eksik veriyi tam sanip EXIT_OK dondururdu
        # -- "islenmemis sembol varken cikis kodu asla 0 olamaz" garantisi
        # tam da burada kirilirdi. Piyasa kosularinda symbol_count=0'dir,
        # yani bu dal is yapmaz.
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
    """Tek shard'li, proxy'siz calistirma (geriye uyumlu giris noktasi).

    Advisory lock burada alinir, CLI katmaninda degil (S8.7): run_sync
    dogrudan cagrildiginda da (kutuphane kullanimi, canli test) es zamanli
    iki calistirmanin ayni satirlara yazmasi engellenir. `acquire_lock`
    yalnizca kilidi disaridan tutan cagiran icin kapatilir.

    Cok shard'li calistirma icin bkz. `yfin.shard.run_sharded`.
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
