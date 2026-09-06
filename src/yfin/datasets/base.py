"""Dataset sozlesmesi (S6.1).

Bu modul SQLAlchemy'ye ve VERITABANINA bagimli DEGILDIR: hangi verinin hangi
tabloya, hangi anahtarlarla ve hangi kolon kapsamiyla yazilacagini tanimlar.
Yazmanin nasil yapildigi `yfin.persistence` icindedir.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from yfin.persistence import RowWriter


class WatermarkProvider(Protocol):
    """Salt-okunur watermark saglayicinin sozlesmesi (PB S6.3).

    `Callable[..., date | datetime | None]` YETERLI DEGILDIR: `...`
    arguman denetimini butunuyle kapatir ve yanlis cagri tipte
    yakalanmaz. Protocol, `where`in adli ve opsiyonel oldugunu da
    belgeler - mevcut cagrilar (history, shares_full) onu vermez.
    """

    def __call__(
        self,
        table: str,
        column: str,
        symbol: str,
        *,
        where: Mapping[str, Any] | None = None,
    ) -> date | datetime | None: ...


class ScopeProvider(Protocol):
    """intraday_scope cozumleyicisi (PB S6.5a)."""

    def __call__(self, symbol: str, interval: str) -> bool: ...


class GapProvider(Protocol):
    """Acik (cozulmemis) bosluklari dondurur (PB S6.2/5)."""

    def __call__(self, symbol: str, interval: str) -> list[tuple[datetime, datetime]]: ...


WriteMode = Literal["upsert", "replace_scope"]

# Bir dataset'in --start/--end aralaigina nasil tepki verdigi (AH S6.2).
#   "api"    : aralik yfinance CAGRISINA gecer -> gercek geriye donuk cekim
#   "filter" : kaynak sabit pencere dondurur; normalize satir eler
#   "none"   : aralik anlamsiz; --start verildiginde dataset KOSTURULMAZ
#
# Ucuncu seviye zorunludur: --start ayni komutta hem `history` (farkli
# cekim) hem `upgrades_downgrades` (satir eleme) uzerinde calisir. Tek bir
# bool ile ayrilsalardi `--start 2020-01-01 --datasets history` Yahoo'dan
# 2020 oncesini cekip SONRA atardi.
DateRange = Literal["api", "filter", "none"]


@dataclass(frozen=True)
class TableWrite:
    table: str
    rows: list[dict[str, Any]]
    key_columns: tuple[str, ...]  # dogrulama sorgusu bunlari kullanir
    update_columns: tuple[str, ...]  # catisma halinde guncellenecek kolonlar
    mode: WriteMode = "upsert"
    # replace_scope silme kapsamini belirleyen kolonlar (S6.2). Varsayilan
    # ("symbol",) mevcut cagirilari degistirmeden birakir.
    scope_columns: tuple[str, ...] = ("symbol",)
    # Kapsam degerleri satirlardan turetilemedigi durumda (ornegin donemin
    # TUM kalemleri NaN geldiginde rows bostur) acikca verilir; verilmezse
    # rows'tan turetilir.
    scope_values: tuple[Mapping[str, Any], ...] | None = None
    # ON DUPLICATE KEY UPDATE'te GREATEST(mevcut, yeni) uygulanacak
    # kolonlar (S6.2). Kaynak ayni satir icin bir kez 1, ertesi kez 0
    # bildirebiliyorsa (price_history.is_repaired) duz upsert bilgiyi
    # geri yazar; monotonik kolon yalnizca yukari yonde ilerler.
    monotonic_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class NormalizedResult:
    writes: list[TableWrite] = field(default_factory=list)
    # hash degismedigi icin yazilmayanlar (tablo -> adet)
    skipped: dict[str, int] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not any(w.rows for w in self.writes) and not self.skipped


@dataclass
class WriteStats:
    attempted: dict[str, int] = field(default_factory=dict)
    verified: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)

    def tables(self) -> list[str]:
        seen: dict[str, None] = {}
        for src in (self.attempted, self.verified, self.skipped):
            for name in src:
                seen[name] = None
        return list(seen)


def _sum_counters(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    """Anahtar bazinda toplar; SIFIR degerli girisi DUSURMEZ.

    Sifiri elemek cazip gorunur ama yanlistir: `AsOfGate.upsert` hash esitken
    satir TASIMAYAN hedefe `attempted.setdefault(table, 0)` yazar. O giris
    kaybolursa tablo `tables()` disinda kalir, `_record_items` onu hic gormez
    ve o tablo o kosuda denetimden duser.
    """
    merged = dict(left)
    for name, count in right.items():
        merged[name] = merged.get(name, 0) + count
    return merged


def merge_stats(left: WriteStats, right: WriteStats) -> WriteStats:
    """Iki `WriteStats`i tek sonuca indirger (SQ S6.2.1).

    `DiscoveryDataset.upsert` yazimlari ikiye boler -- kapi disi tablolar
    (`symbols`, `news`, `news_symbols`, `research_reports`) duz upsert edilir,
    kalanlar `AsOfGate`e delege edilir -- ve iki ayri istatistik uretir.
    `_record_items` ise tek bir `WriteStats` bekler.

    Girdiler DEGISTIRILMEZ: `apply_write` cagrilari sirasinda ayni sozlukler
    hala kullanimda olabilir.
    """
    return WriteStats(
        attempted=_sum_counters(left.attempted, right.attempted),
        verified=_sum_counters(left.verified, right.verified),
        skipped=_sum_counters(left.skipped, right.skipped),
    )


class SyncContext:
    """Sembol kapsamli, WORKER'A AIT baglam (S6.1).

    Paylasilmadigi icin `cached` uzerinde kilide gerek yoktur; sembol
    islendikten sonra butunuyle atilir.
    """

    def __init__(
        self,
        symbol: str,
        ticker: Any,
        fetched_at: datetime,
        *,
        watermark_provider: WatermarkProvider | None = None,
        scope_provider: ScopeProvider | None = None,
        gap_provider: GapProvider | None = None,
        full_refresh: bool = False,
        selected: frozenset[str] | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> None:
        self.symbol = symbol
        self.ticker = ticker
        self.fetched_at = fetched_at
        self.full_refresh = full_refresh
        # --start/--end (AH S6.2). None/None = filtresiz: Yahoo'nun verdigi
        # TUM gecmis yazilir. Aralik daha fazla veri GETIRMEZ, kapsami
        # daraltir; "api" dataset'lerinde watermark'i gecersiz kilar.
        self.start = start
        self.end = end
        # Bu calistirmada secili dataset adlari. Paylasilan history
        # cercevesinin `start`'i, cerceveyi TUKETEN tablolarin
        # watermark'larinin minimumudur (S6.3); None = "hepsi secili
        # varsay" (kutuphane kullanimi ve testler icin guvenli taraf).
        self.selected = selected
        self._cache: dict[str, Any] = {}
        self._watermark_provider = watermark_provider
        self._scope_provider = scope_provider
        self._gap_provider = gap_provider

    def cached(self, key: str, fn: Callable[[], Any]) -> Any:
        """Fetch onbellegi: symbols, fast_info ve history_metadata ayni iki
        cagriyi paylasir; capital_gains history ile ayni onbellekten gelir."""
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def watermark(
        self,
        table: str,
        column: str,
        *,
        where: Mapping[str, Any] | None = None,
    ) -> date | datetime | None:
        """Salt-okunur DB sorgusu; sozlesme fetch icinde buna acikca izin
        verir (S7.3). --full-refresh watermark'lari atlar.

        `where` ek esitlik kosullaridir. price_bars'ta ZORUNLUDUR: tek bir
        MAX(ts_utc) 1m ile 60m'yi karistirir ve 60m guncel sanilip ilk
        dolumu hic yapilmaz (PB S6.3). Verilmezse davranis eskisiyle
        birebir aynidir.
        """
        if self.full_refresh or self._watermark_provider is None:
            return None
        return self._watermark_provider(table, column, self.symbol, where=where)

    def in_scope(self, interval: str) -> bool:
        """Sembol bu interval icin kapsamda mi (PB S6.5).

        Saglayici yoksa TRUE: kutuphane kullanimi ve testler icin guvenli
        taraf, cunku aksi halde dogrudan cagrilan bir dataset sessizce
        hicbir sey yapmazdi.
        """
        if self._scope_provider is None:
            return True
        return self._scope_provider(self.symbol, interval)

    def open_gaps(self, interval: str) -> list[tuple[datetime, datetime]]:
        """Bu sembol/interval icin cozulmemis bosluklar (PB S6.2/5)."""
        if self._gap_provider is None:
            return []
        return self._gap_provider(self.symbol, interval)


class Dataset[RawT](ABC):
    """Tek bir yfinance API'sinin cekilmesi, normalize edilmesi ve yazilmasi.

    `RawT` fetch'in dondurdugu tiptir; normalize ayni tipi alir. Boylece
    iki adim arasindaki sozlesme mypy tarafindan dogrulanabilir.
    """

    # `name` sinif attribute'u olarak TANIMLIDIR ama alt tip onu instance
    # attribute'una cevirebilir (IntervalBarDataset alti interval icin tek
    # sinif kullanir). Registrable protokolu `name: str` istedigi icin
    # ikisi de gecerlidir ve sozlesme DARALMAZ - `produces` dersinden
    # (S6.1/6) farki budur, orada tipin kendisi degisiyordu.
    name: str
    depends_on: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()  # yazdigi TABLO adlari (S6.1/1)
    # Class attribute'tur, property DEGIL: alt tipte property'ye cevrilirse
    # tabanin sozlesmesi daralir (S6.1/6'daki `produces` dersinin aynisi).
    date_range: DateRange = "none"  # AH S6.2

    @abstractmethod
    def fetch(self, ctx: SyncContext) -> RawT:
        """Ham veriyi ceker.

        `DatasetOutOfScope` FIRLATABILIR (PB S6.5): dataset o sembol icin
        bilerek kosturulmadi demektir ve runner bunu jenerik hata
        yolundan ONCE yakalayip OUT_OF_SCOPE olarak kaydeder. Sozlesmenin
        parcasidir; alt tipler bunu serbestce kullanabilir.
        """
        ...

    @abstractmethod
    def normalize(self, raw: RawT, symbol: str) -> NormalizedResult: ...

    def upsert(self, writer: RowWriter, result: NormalizedResult) -> WriteStats:
        """Varsayilan uygulama: her TableWrite icin idempotent upsert +
        anahtar varligi dogrulamasi (S7.2, S8.6)."""
        from yfin.persistence import apply_write

        stats = WriteStats(skipped=dict(result.skipped))
        for write in result.writes:
            apply_write(writer, write, stats)
        return stats
