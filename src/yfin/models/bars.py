"""Cok interval'li fiyat barlari ve onlarin denetim tablolari (PB S5).

price_history'nin (interval='1d') kardesidir, kopyasi DEGILDIR: anahtar
semantigi farklidir. price_history satirinin otoritesi borsanin YEREL
SEANS TARIHIDIR (S5.4); price_bars satirininki MUTLAK ZAMAN DAMGASIDIR.
GC=F olcumu farki gosterir - bari 18:10'da acilir, yani barin yerel
takvim gunu seans gunu DEGILDIR.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Index, String
from sqlalchemy.dialects.mysql import BIGINT
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import (
    AsciiKeyType,
    BarIntervalType,
    Base,
    PriceType,
    SymbolType,
    TsType,
    symbol_fk_column,
)

# Yazilan interval'lerin TEK KAYNAGI. Disarida birakilanlar ve gerekceleri
# PB S1'de: 2m (1m'den turetilebilir), 30m (Yahoo'dan gelmiyor, 15m'den
# resample), 90m (standart disi), 1h (60m ile ayni), 5d (bozuk),
# 3mo (repair ile hizalamasi aya gore kayiyor).
BAR_INTERVALS: tuple[str, ...] = ("1m", "5m", "15m", "60m", "1wk", "1mo")

# Gun ici interval'ler. Ayri tutulur cunku iki yerde SEMANTIK fark yaratir:
#   1. is_extended yalniz bunlar icin anlamlidir (PB S6.4 kural 1)
#   2. rescale UPDATE'i YALNIZ bunlara uygulanir (PB S6.6/1): 1wk ve 1mo
#      her kosuda period="max" ile bastan cekilir, yani daima Yahoo'nun
#      guncel olceginde gelir. Onlari olceklemek, o kosuda fetch duserse
#      satirlari CIFT duzeltilmis birakir ve bar_rescales split'i
#      "uygulandi" saydigi icin bir daha duzelmez.
INTRADAY_INTERVALS: tuple[str, ...] = ("1m", "5m", "15m", "60m")

# bar_gaps.reason degerleri
GAP_RETENTION_EXPIRED = "retention_expired"
GAP_FETCH_FAILED = "fetch_failed"


class PriceBar(Base):
    """price_history'nin intraday/cok-gunluk kardesi.

    FK YOKTUR: MySQL 8 partition'li InnoDB tablosunda foreign key
    desteklemez (ERROR 1506). `symbol` yine symbols.symbol'a isaret eder;
    butunluk yazim yolunda (symbols bootstrap'i once kosar) ve aylik
    oksuz satir sorgusuyla korunur (PB S7.5/2, PB S8.7).
    """

    __tablename__ = "price_bars"
    __table_args__ = (
        Index("ix_price_bars_local_date", "local_date"),
    )

    # symbol_fk_column DEGIL (FK tasimaz), ama TIPI birebir aynidir:
    # farkli genislik/collation ileride symbols ile JOIN'de ERROR 3780
    # uretirdi (S5.1).
    symbol: Mapped[str] = mapped_column(SymbolType(), primary_key=True)
    bar_interval: Mapped[str] = mapped_column(BarIntervalType(), primary_key=True)
    ts_utc: Mapped[datetime] = mapped_column(TsType(), primary_key=True)

    # TUREV kolon: barin YEREL TAKVIM TARIHI. Adi bilerek session_date
    # DEGILDIR - price_history.session_date bir SEANS gunudur, bu ise
    # yalnizca yerel takvim gunu. Ayni adi tasisalardi iki farkli kavram
    # karisirdi (GC=F: 18:10 bari, seansi ertesi gune ait).
    local_date: Mapped[date] = mapped_column(nullable=False)

    open: Mapped[Decimal | None] = mapped_column(PriceType())
    high: Mapped[Decimal | None] = mapped_column(PriceType())
    low: Mapped[Decimal | None] = mapped_column(PriceType())
    close: Mapped[Decimal] = mapped_column(PriceType(), nullable=False)
    volume: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))

    # Seans disi (pre/post market) bari mi. Tek dogruluk kaynagi
    # tradingPeriods'in start/end araligidir (PB S6.4);
    # has_pre_post_market_data KULLANILMAZ - SHEL.L ve VWCE.DE onu False
    # bildirdikleri halde seans disi bar donduruyor.
    is_extended: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")


class IntradayScope(Base):
    """1m (ve istenirse diger interval'lerin) sembol alt kumesi.

    Bu liste KONFIGURASYON DEGIL VERIDIR: 5.000 sembollu evrende 500
    sembollu bir alt kume .env'e sigmaz, surumlenmesi ve degistirilmesi
    gereken bir tablodur (PB K6).
    """

    __tablename__ = "intraday_scope"

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    bar_interval: Mapped[str] = mapped_column(BarIntervalType(), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="1")
    added_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    note: Mapped[str | None] = mapped_column(String(255, collation="C"))


class BarGap(Base):
    """Kacirilan pencerelerin KALICI kaydi.

    Kalici arsivde "veri yok" iki farkli sey olabilir: piyasa kapaliydi,
    ya da biz kacirdik. Bu ayrim SONRADAN yapilamaz - Yahoo penceresi
    gectikten sonra "burada bar var miydi" sorusunun cevabi yoktur.
    Yazildigi anda kaydedilmezse bilgi geri gelmez (PB K10).
    """

    __tablename__ = "bar_gaps"
    __table_args__ = (Index("ix_bar_gaps_detected_at", "detected_at"),)

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    bar_interval: Mapped[str] = mapped_column(BarIntervalType(), primary_key=True)
    gap_start_utc: Mapped[datetime] = mapped_column(TsType(), primary_key=True)
    gap_end_utc: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    reason: Mapped[str] = mapped_column(AsciiKeyType(24), nullable=False)
    # NULL = bosluk HALA ACIK. Planlayici acik bosluklari her kosuda
    # yeniden dener (PB S6.2/5); bu geri besleme olmadan bar_gaps yalnizca
    # bir mezar tasi olurdu: ortadaki bir dilim dusup sonrakiler
    # yazildiginda watermark boslugun OTESINE gecer ve o pencere bir daha
    # hic istenmezdi. retention_expired satirlarinda DAIMA NULL kalir.
    resolved_at: Mapped[datetime | None] = mapped_column(TsType())


class BarRescale(Base):
    """Uygulanan geriye donuk olceklemelerin defteri.

    IDEMPOTENCY KAPISIDIR: bu tablo olmadan ayni split ikinci kosuda
    tekrar uygulanir ve arsiv bir kez daha bozulur. UPDATE ile bu kayit
    AYNI TRANSACTION'DADIR (PB S6.6/9).

    KURULUM TOHUMU ZORUNLUDUR: splits tablosu mevcut hat tarafindan zaten
    doludur. Bos bir bar_rescales ile yapilan ILK KOSU tum tarihsel
    split'leri uygular ve ornegin AAPL arsivini 2*2*2*7*4 = 224'e boler -
    oysa o barlar Yahoo'dan zaten guncel olcekte gelmistir. `yfin rescale
    --seed` her mevcut split icin ratio=1 baseline kaydi yazar
    (PB S6.6, PB S10/9a).
    """

    __tablename__ = "bar_rescales"

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    split_date: Mapped[date] = mapped_column(primary_key=True)
    # DECIMAL, float DEGIL: 3:2 split'te ratio 1.5'tir ve float bolme
    # milyonlarca satirda birikimli sapma uretir.
    ratio: Mapped[Decimal] = mapped_column(PriceType(), nullable=False)
    applied_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    rows_affected: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)


def price_bars_partition_ddl(
    *, first: tuple[int, int] = (2023, 10), last: tuple[int, int] = (2028, 9)
) -> str:
    """price_bars'in RANGE COLUMNS(ts_utc) partition DDL'i.

    Alembic partition DDL'ini autogenerate EDEMEZ; hem migration hem test
    conftest'i bu ayni sabiti kullanir (proje V_ACTIONS_CREATE icin de
    boyle yapiyor). Aksi halde testler PARTITION'SIZ bir tabloya karsi
    kosar ve pruning davranisi hic dogrulanamaz.

    MAXVALUE BOLUMU YOKTUR ve bu bilincli bir karardir. Olculdu:
      - MAXVALUE varken ADD PARTITION IMKANSIZDIR (ERROR 1493); tek yol
        REORGANIZE PARTITION'dir ve o, p_future'daki HER SATIRI kopyalar
        (ALGORITHM=INPLACE/LOCK=NONE secenegi yoktur, ERROR 1064).
        Bakim birkac ay atlanirsa onarim, yuz milyonlarca satirin kilit
        altinda yeniden yazilmasi demektir.
      - MAXVALUE yokken aralik disi insert ERROR 1526 ile GURULTULU
        duser ve ADD PARTITION metadata-only, anliktir.
    Sessiz pruning olumu yerine gurultulu insert hatasi tercih edilir:
    hata, bakimin atlandigini kosunun kendisinde bildirir.

    Aralik ileriye VE geriye acilir: ilk dolum 60m icin 729 gun veri
    getirir (PB S4.3), tek bir baslangic partition'i olsaydi tum gecmis
    oraya duser ve pruning gecmis sorgularinda hic calismazdi. 1wk/1mo'nun
    period="max" ile gelen daha eski satirlari icin tek bir p_hist yeterli
    - bu iki interval'in satir sayisi ihmal edilebilir (PB S5.2).
    """
    parts = [f"  PARTITION p_hist VALUES LESS THAN ('{first[0]:04d}-{first[1]:02d}-01')"]
    year, month = first
    while (year, month) <= last:
        nyear, nmonth = (year + 1, 1) if month == 12 else (year, month + 1)
        parts.append(
            f"  PARTITION p{year:04d}_{month:02d} VALUES LESS THAN ('{nyear:04d}-{nmonth:02d}-01')"
        )
        year, month = nyear, nmonth
    body = ",\n".join(parts)
    return f"ALTER TABLE price_bars\nPARTITION BY RANGE COLUMNS (ts_utc) (\n{body}\n)"
