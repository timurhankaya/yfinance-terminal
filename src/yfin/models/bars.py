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

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import (
    AsciiKeyType,
    BarIntervalType,
    Base,
    PriceType,
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

# Gun ustu interval'ler. AYRI BIR TABLOYA yazilirlar (`periodic_bars`) ve
# bu bir depolama optimizasyonu DEGIL, olculmus bir zorunluluktur:
#
#   1wk/1mo satirlarin %0,07'sidir (5.000 sembolde ~320 bin satir/yil)
#   ama zaman araliginin %100'udur: `period="max"` ile 1980'e kadar iner.
#   Ayni hypertable'da 7 gunluk chunk araligiyla 16.700 / 7 = ~2.386 chunk
#   dogururlar -- olculdu: tek sembolde 21.934 satir icin 2.388 chunk,
#   chunk basina ~9 satir. Yani chunk patlamasinin TAMAMINI verinin binde
#   yedisi uretiyordu.
#
# Ayrilinca `price_bars`in araligi 60m'in 729 gunune iner (~104 chunk) ve
# `periodic_bars` hypertable OLMAZ: 46 yillik dolum ~15 milyon satirdir,
# chunk'lamaya ihtiyaci yoktur.
#
# MySQL bunu `p_hist` adinda tek bir tarihsel partition'la cozuyordu;
# TimescaleDB'de o kavramin karsiligi yoktur, ayirmak gerekir.
PERIODIC_INTERVALS: tuple[str, ...] = ("1wk", "1mo")


def bars_table_for(interval: str) -> str:
    """Interval'i yazilacagi TABLOYA cozer (PB S5, PG S7.1).

    Tek dogruluk kaynagi: hem dataset yazimi hem watermark okumasi hem
    testler bunu kullanir. Ayrisirlarsa bir interval yanlis tabloya
    yazilir ve watermark hep NULL kalir -- yani her kosuda bastan dolum.
    """
    return "price_bars" if interval in INTRADAY_INTERVALS else "periodic_bars"

# bar_gaps.reason degerleri
GAP_RETENTION_EXPIRED = "retention_expired"
GAP_FETCH_FAILED = "fetch_failed"


class PriceBar(Base):
    """INTRADAY barlar (1m/5m/15m/60m). price_history'nin kardesi.

    FK TASIR. MySQL 8'de partition'li InnoDB tablosu foreign key
    desteklemiyordu (ERROR 1506) ve butunluk yazim yolunda + aylik bir
    oksuz-satir sorgusuyla korunuyordu. TimescaleDB hypertable'i
    REFERENCING taraf OLABILIR (olculdu: ON UPDATE CASCADE +
    ON DELETE RESTRICT calisiyor, giden FK varken drop_chunks sorunsuz),
    bu yuzden butunluk artik DB seviyesindedir ve oksuz-satir sorgusu
    GEREKSIZDIR (PG S7.2).

    Kabul edilen bedel: her insert `symbols` satirinda paylasimli kilit
    alir ve price_bars en yogun yazilan tablodur.

    YALNIZ INTRADAY: 1wk/1mo `periodic_bars`a gider (bkz.
    PERIODIC_INTERVALS). Ayni tabloda olsalardi 46 yillik zaman
    araliklariyla chunk sayisini yirmi kat sisirirlerdi.
    """

    __tablename__ = "price_bars"
    __table_args__ = (
        Index("ix_price_bars_local_date", "local_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
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
    volume: Mapped[int | None] = mapped_column(
        BigInteger, CheckConstraint('"volume" >= 0', name="ck_price_bars_volume_nonneg")
    )

    # Seans disi (pre/post market) bari mi. Tek dogruluk kaynagi
    # tradingPeriods'in start/end araligidir (PB S6.4);
    # has_pre_post_market_data KULLANILMAZ - SHEL.L ve VWCE.DE onu False
    # bildirdikleri halde seans disi bar donduruyor.
    is_extended: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class IntradayScope(Base):
    """1m (ve istenirse diger interval'lerin) sembol alt kumesi.

    Bu liste KONFIGURASYON DEGIL VERIDIR: 5.000 sembollu evrende 500
    sembollu bir alt kume .env'e sigmaz, surumlenmesi ve degistirilmesi
    gereken bir tablodur (PB K6).
    """

    __tablename__ = "intraday_scope"

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    bar_interval: Mapped[str] = mapped_column(BarIntervalType(), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    added_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    note: Mapped[str | None] = mapped_column(String(255, collation="C"))


class PeriodicBar(Base):
    """Gun ustu barlar (1wk/1mo). HYPERTABLE DEGILDIR.

    `price_bars`tan AYRI durur cunku iki tablo her operasyonel boyutta
    ayrisir:
      * ZAMAN ARALIGI: burasi 46 yil, orasi 729 gun.
      * YOGUNLUK: burasi 5.000 sembolde ~320 bin satir/yil, orasi ~464
        milyon.
      * RESCALE: geriye donuk olcekleme YALNIZ intraday'e uygulanir
        (PB S6.6/1) -- bu tablo her kosuda period="max" ile bastan
        cekildigi icin daima Yahoo'nun guncel olceginde gelir.
      * is_extended: seans disi kavrami gun ustu barda ANLAMSIZDIR, bu
        yuzden KOLON YOKTUR.

    Chunk'lanmaz: 46 yillik dolum ~15 milyon satirdir ve PK indeksi
    yeter. Hypertable yapmak, cozdugumuz chunk patlamasini geri
    getirirdi.
    """

    __tablename__ = "periodic_bars"
    __table_args__ = (
        Index("ix_periodic_bars_local_date", "local_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    bar_interval: Mapped[str] = mapped_column(BarIntervalType(), primary_key=True)
    ts_utc: Mapped[datetime] = mapped_column(TsType(), primary_key=True)
    local_date: Mapped[date] = mapped_column(nullable=False)

    open: Mapped[Decimal | None] = mapped_column(PriceType())
    high: Mapped[Decimal | None] = mapped_column(PriceType())
    low: Mapped[Decimal | None] = mapped_column(PriceType())
    close: Mapped[Decimal] = mapped_column(PriceType(), nullable=False)
    volume: Mapped[int | None] = mapped_column(
        BigInteger, CheckConstraint('"volume" >= 0', name="ck_periodic_bars_volume_nonneg")
    )


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
    rows_affected: Mapped[int] = mapped_column(
        BigInteger,
        CheckConstraint('"rows_affected" >= 0', name="ck_bar_rescales_rows_affected_nonneg"),
        nullable=False,
    )


def timescale_ddl() -> tuple[str, ...]:
    """price_bars ve price_history icin hypertable DDL'i.

    Alembic bunu autogenerate EDEMEZ; hem migration hem test conftest'i
    BU AYNI SABITI kullanir (projenin V_ACTIONS_CREATE icin kurdugu
    desen). Aksi halde testler hypertable'siz DUZ tablolara karsi kosar
    ve chunk davranisi hic dogrulanmaz.

    `create_default_indexes => FALSE` ZORUNLUDUR. Varsayilan davranis
    bolumleme kolonu uzerinde `price_bars_ts_utc_idx` adli bir DESC
    indeks yaratir; o indeks `public` semasinda durur, Base.metadata'da
    YOKTUR ve Alembic autogenerate onu "silinmeli" diye raporlar -- yani
    `yfin db revision`in "bos diff" kapisi HIC acilmaz (PG S7.1).
    Gereken indeksler modelde acikca tanimlidir
    (ix_price_bars_local_date, ix_price_history_session_date); ts_utc
    PK'nin son bileseni oldugu icin ayri indeks gerekmez.

    INTERVAL '1 year' KULLANILMAZ: TimescaleDB ay iceren interval'i 30
    gunluk aylara cevirir ve aralik 360 gun olarak kaydolur (olculdu).

    `periodic_bars` BU LISTEDE YOKTUR ve olmamalidir: 1wk/1mo 46 yil
    kapsar ama ~15 milyon satirdir; hypertable yapmak chunk patlamasini
    geri getirirdi (PG S7.1, PERIODIC_INTERVALS notu).

    Aylik partition'lari ELLE eklemek gerekmez: chunk'lar yazma aninda
    olusur. "Aralik disi insert" kavrami YOKTUR, dolayisiyla MySQL'deki
    "gurultulu ERROR 1526 mi sessiz pruning olumu mu" ikilemi de ortadan
    kalkar -- bu, migrasyonun en buyuk tek kazancidir.
    """
    return (
        "SELECT create_hypertable('price_bars', "
        "by_range('ts_utc', INTERVAL '7 days'), "
        "create_default_indexes => FALSE)",
        "SELECT create_hypertable('price_history', "
        "by_range('session_date', INTERVAL '365 days'), "
        "create_default_indexes => FALSE)",
    )
