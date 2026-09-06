"""Ortak DeclarativeBase ve tip fabrikalari (PG S2)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import VARCHAR, ForeignKey, MetaData, Numeric, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, MappedColumn, mapped_column

# Kisit ADLARI deterministik olmak ZORUNDADIR. Adsiz birakilan bir
# CheckConstraint/UniqueConstraint icin Alembic her calistirmada farkli bir
# ad uretir ve `yfin db revision` sahte bir fark raporlar -- yani
# "bos diff" kapisi (PG S14 adim 7) HIC acilmaz. Ad uretimini SQLAlchemy'ye
# devretmek, kisitlari tek tek adlandirmaktan da saglamdir: yeni bir kisit
# ekleyen kimse adlandirmayi unutamaz.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Tum tablolarin ortak tabani."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# --- collation: her yerde "C" ---------------------------------------------
#
# MySQL'de her kolonun kendi charset/collation'i vardi ve varsayilan
# utf8mb4_0900_ai_ci BUYUK/KUCUK HARF DUYARSIZDI. PostgreSQL'de tek bir
# secim yapildi: COLLATE "C". Byte siralidir, duyarlidir ve `ascii_bin`
# ile `utf8mb4_0900_as_cs`in birebir karsiligidir.
#
# "C" ayrica LIKE icin indeks kullanilabilir kilar: olculdu, "C"
# kolonunda `LIKE 'abc%'` `text_pattern_ops` OLMADAN Index Scan uretti,
# ayni tabloda en_US.utf8 kolonunda ayni sorgu Seq Scan'e dustu.
#
# Kaybedilen duyarsizlik semantigi KODA tasindi (PG S2.5): sembol alanlari
# yazilirken .upper(), proxy hostname'i yazilirken .lower() uygulanir.


# Sembol ve sembol-benzeri anahtarlarin ORTAK uzunlugu. Tek sayi olmasi
# zorunludur: `sync_run_items.symbol` denetim kaydini da bu tip tutar ve
# daha uzun bir anahtar orada deger tasmasi verirdi -- veri YAZILDIKTAN
# SONRA, yani kosunun en gec aninda (SQ denetimi).
SYMBOL_LENGTH = 32


def SymbolType() -> VARCHAR:  # noqa: N802
    """VARCHAR(32) COLLATE "C".

    Buyuk/kucuk harf duyarsiz bir collation sembol anahtari icin
    KULLANILAMAZ ('AAPL' = 'aapl' -> tekillik ihlali). FK kolonlarinin
    collation'i ebeveynle esdeger olmalidir; PostgreSQL bunu MySQL gibi
    hata ile ZORLAMAZ, yani sapma SESSIZDIR ve invaryant testiyle
    korunur (tests/unit/test_schema_invariants.py).
    """
    return VARCHAR(SYMBOL_LENGTH, collation="C")


def NewsIdType() -> VARCHAR:  # noqa: N802
    """CHAR(36) esdegeri; news id'leri sabit 36 karakterlik UUID."""
    return VARCHAR(36, collation="C")


def HashType() -> VARCHAR:  # noqa: N802
    """SHA-256 hex; 256 yerine 64 byte."""
    return VARCHAR(64, collation="C")


def PersonNameType() -> VARCHAR:  # noqa: N802
    """'Tim Cook' != 'TIM COOK' olmali (S5.1)."""
    return VARCHAR(255, collation="C")


def ShortHashType() -> VARCHAR:  # noqa: N802
    """SHA-256'nin ilk 16 hanesi; PK bilesenidir.

    Duyarsiz bir collation buyuk/kucuk harf ayrimini yutar, CHAR'in
    pad-space semantigi de sondaki boslugu kirpardi; ikisi de PK'da
    sessiz satir kaybi demektir. VARCHAR + "C" ikisini de onler.
    """
    return VARCHAR(16, collation="C")


def BarIntervalType() -> VARCHAR:  # noqa: N802
    """Bar interval kodu: '1m', '5m', '15m', '60m', '1wk', '1mo'.

    ENUM DEGILDIR. Gerekce PostgreSQL'de MOTORDAN degil, tek dogruluk
    kaynagi ilkesinden gelir: gecerli deger kumesi Python tarafinda
    `BAR_INTERVALS`tedir (models/bars.py) ve semada tekrarlanmasi iki
    kaynagin sessizce ayrismasi demektir. (MySQL'de ikinci bir gerekce
    vardi -- ENUM'a deger eklemek 464 milyon satirli tabloda ALTER TABLE
    demekti; PostgreSQL'de `ALTER TYPE ... ADD VALUE` ucuzdur, yani o
    gerekce DUSTU. Karar ilkiyle ayakta kalir.)

    KOLON ADI `bar_interval`, `interval` DEGIL: INTERVAL PostgreSQL'de de
    bir TIP ADIDIR. SQLAlchemy kolonu otomatik tirnaklar, ama view tanimi
    ve rescale UPDATE'i HAM SQL'dir; orada tirnak bir gun unutulur ve
    hata uretim aninda cikar.

    COLLATE "C": PK bilesenidir; duyarsiz bir collation '1M' = '1m'
    sayardi -- sessiz satir kaybi.
    """
    return VARCHAR(4, collation="C")


def RegionType() -> VARCHAR:  # noqa: N802
    """Piyasa bolge kodu (US, EUROPE, CRYPTOCURRENCIES...).

    Uc tabloda da ayni tip kullanilir; genislik farki ileride FK veya
    JOIN gerektiginde semantik sapma uretirdi.
    """
    return VARCHAR(16, collation="C")


def KeyTextType(length: int) -> VARCHAR:  # noqa: N802
    """PK'ya giren serbest metin.

    Duyarsiz bir collation 'Enflasyon' = 'ENFLASYON' sayar ve iki farkli
    olayi tek satira indirirdi (S5).
    """
    return VARCHAR(length, collation="C")


def AsciiKeyType(length: int) -> VARCHAR:  # noqa: N802
    """PK'ya giren ASCII kod alani (filing_type, action, board_code)."""
    return VARCHAR(length, collation="C")


def ProxyLabelType() -> VARCHAR:  # noqa: N802
    """proxies.label ve sync_run_items.proxy_label.

    UNIQUE anahtar oldugu icin duyarli: 'eu-1' != 'EU-1' olmali.
    """
    return VARCHAR(64, collation="C")


def HostType() -> VARCHAR:  # noqa: N802
    """Proxy hostname veya IP. VARCHAR(255) COLLATE "C".

    Hostname'ler buyuk/kucuk harf duyarsizdir (RFC 4343). MySQL bunu
    `ascii_general_ci` ile SEMADA sagliyordu; PostgreSQL'de duyarsizlik
    YAZMA YOLUNDA saglanir -- host `.lower()` ile normalize edilir
    (scripts/seed_proxies.py, PG S2.5.2). `uq_proxies_endpoint` boylece
    semantigini korur.
    """
    return VARCHAR(255, collation="C")


# --- S5.4 tip kararlari ----------------------------------------------------

PRICE_PRECISION = 28
PRICE_SCALE = 12
BIG_PRECISION = 38
FACT_PRECISION = 38
FACT_SCALE = 10


def PriceType() -> Numeric[Decimal]:  # noqa: N802
    """Fiyat ve oranlar. DB'de round-trip kaybi olmadigi dogrulandi."""
    return Numeric(PRICE_PRECISION, PRICE_SCALE, asdecimal=True)


def FactValueType() -> Numeric[Decimal]:  # noqa: N802
    """Finansal tablo kalem degeri (S5.6).

    Ayni kolonda hem 1.06e14 (7203.T toplam varlik) hem 0.156
    (TaxRateForCalcs) bulunur; NUMERIC(38,0) oranlari yok ederdi.

    PostgreSQL NUMERIC(38,10) 11. basamagi SESSIZCE yuvarlar (olculdu:
    numeric(5,2) <- 1.239 -> 1.24, uyari yok) -- MySQL Note 1265 ile ayni
    davranis. Bu yuzden yuvarlama Python tarafinda quantize ile BILINCLI
    yapilir. (Tam sayi kismi tastiginda ise gurultulu hata gelir: 22003.)
    """
    return Numeric(FACT_PRECISION, FACT_SCALE, asdecimal=True)


def BigNumType() -> Numeric[Decimal]:  # noqa: N802
    """marketCap / totalRevenue / enterpriseValue gibi buyuk degerler.
    'Tum sayisal info alanlari NUMERIC(28,12)' kestirmesi tasma verir."""
    return Numeric(BIG_PRECISION, 0, asdecimal=True)


def TsType() -> TIMESTAMP:  # noqa: N802
    """Tum zaman damgalari TIMESTAMP(6) WITH TIME ZONE.

    DIALECT TIPI ZORUNLUDUR: generic `sqlalchemy.TIMESTAMP` `precision`
    argumanini KABUL ETMEZ (TypeError).

    Alti hane SART: saniye hassasiyeti ayni saniyede PK cakismasi uretir
    (ticker_info_history PK'si (symbol, fetched_at)) ve PostgreSQL
    kesirleri YUVARLAR, kesmez (olculdu: timestamptz(0) ile .9 -> +1 sn).

    Kolon tz TASIR: MySQL DATETIME tasimadigi icin normalize damgayi
    naive'e indiriyordu; artik UTC-aware deger yazilir (PG S2.3).
    """
    return TIMESTAMP(timezone=True, precision=6)


def RawJsonType() -> Text:  # noqa: N802
    """raw_json TEXT'tir, JSON/JSONB DEGIL (PG S2.4).

    MySQL'de LONGTEXT secilmesinin uc gerekcesi PostgreSQL `jsonb` icin
    AYNEN gecerlidir: anahtar sirasini degistirir (hash yeniden
    hesaplanamaz), NaN iceren govdeyi reddeder ve sayilari normalize eder
    (0.001870 -> 0.00187). `json` tipi metni korur ama yine sozdizimi
    dogrular ve NaN'i reddeder. TEXT byte-for-byte sadiktir ve
    content_hash'in on kosuludur.
    """
    return Text()


def symbol_fk_column(**kwargs: Any) -> MappedColumn[str]:
    """symbols.symbol'a FK tasiyan sembol kolonu (S5.5).

    ON UPDATE CASCADE ON DELETE RESTRICT: tek bir DELETE 40 yillik gecmisi
    geri donusumsuz silmesin diye soft-delete politikasi DB seviyesinde
    zorlanir.
    """
    return mapped_column(
        SymbolType(),
        ForeignKey("symbols.symbol", onupdate="CASCADE", ondelete="RESTRICT"),
        **kwargs,
    )
