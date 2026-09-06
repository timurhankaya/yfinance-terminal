"""Ortak DeclarativeBase ve tip fabrikalari (S5.1, S5.4)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import ForeignKey, Numeric
from sqlalchemy.dialects.mysql import DATETIME, LONGTEXT, VARCHAR
from sqlalchemy.orm import DeclarativeBase, MappedColumn, mapped_column

MYSQL_TABLE_ARGS: dict[str, str] = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_ai_ci",
}


class Base(DeclarativeBase):
    """Tum tablolarin ortak tabani."""


# --- S5.1 collation istisnalari -------------------------------------------


def SymbolType() -> VARCHAR:  # noqa: N802
    """VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin.

    utf8mb4_0900_ai_ci case-insensitive oldugu icin sembol anahtari icin
    KULLANILAMAZ ('AAPL' = 'aapl' -> Duplicate entry). Ayrica FK
    kolonlarinin collation'i ebeveynle birebir esdeger olmalidir
    (aksi halde ERROR 3780), bu yuzden her sembol kolonu bunu kullanir.
    """
    return VARCHAR(32, charset="ascii", collation="ascii_bin")


def NewsIdType() -> VARCHAR:  # noqa: N802
    """CHAR(36) esdegeri; news id'leri sabit 36 karakterlik UUID."""
    return VARCHAR(36, charset="ascii", collation="ascii_bin")


def HashType() -> VARCHAR:  # noqa: N802
    """SHA-256 hex; 256 yerine 64 byte."""
    return VARCHAR(64, charset="ascii", collation="ascii_general_ci")


def PersonNameType() -> VARCHAR:  # noqa: N802
    """'Tim Cook' != 'TIM COOK' olmali (S5.1)."""
    return VARCHAR(255, charset="utf8mb4", collation="utf8mb4_0900_as_cs")


def ShortHashType() -> VARCHAR:  # noqa: N802
    """SHA-256'nin ilk 16 hanesi; PK bileseni oldugu icin ascii_bin.

    CHAR(16) ascii_general_ci hem buyuk/kucuk harf ayrimini yutar hem
    CHAR'in pad-space semantigiyle sondaki bosluğu kirpar; ikisi de PK'da
    sessiz satir kaybi demektir (S5).
    """
    return VARCHAR(16, charset="ascii", collation="ascii_bin")


def BarIntervalType() -> VARCHAR:  # noqa: N802
    """Bar interval kodu: '1m', '5m', '15m', '60m', '1wk', '1mo'.

    ENUM DEGILDIR: ENUM'a deger eklemek ALTER TABLE'dir ve price_bars
    birinci yil sonunda ~464 milyon satirdir; boyle bir tabloda ALTER
    cok uzun surer. VARCHAR(4) yeni bir interval'i migration'siz kabul
    eder; gecerli deger kumesi Python tarafinda BAR_INTERVALS'tedir
    (models/bars.py).

    KOLON ADI `bar_interval`, `interval` DEGIL: INTERVAL MySQL'de
    REZERVE KELIMEDIR. SQLAlchemy kolonu otomatik tirnaklar, ama view
    tanimi ve rescale UPDATE'i HAM SQL'dir; orada backtick bir gun
    unutulur ve hata uretim aninda cikar.

    ascii_bin: PK bilesenidir; ascii_general_ci '1M' = '1m' sayar ve
    CHAR'in pad-space semantigi sondaki bosluğu kirpardi - ikisi de
    sessiz satir kaybi demektir (S5.1).
    """
    return VARCHAR(4, charset="ascii", collation="ascii_bin")


def RegionType() -> VARCHAR:  # noqa: N802
    """Piyasa bolge kodu (US, EUROPE, CRYPTOCURRENCIES...).

    Uc tabloda da ayni tip kullanilir; genislik farki ileride FK veya JOIN
    gerektiginde ERROR 3780 uretirdi.
    """
    return VARCHAR(16, charset="ascii", collation="ascii_bin")


def KeyTextType(length: int) -> VARCHAR:  # noqa: N802
    """PK'ya giren serbest metin.

    Varsayilan utf8mb4_0900_ai_ci 'Enflasyon' = 'Enflasyon' = 'ENFLASYON'
    sayar ve iki farkli olayi tek satira indirir (S5).
    """
    return VARCHAR(length, charset="utf8mb4", collation="utf8mb4_0900_as_cs")


def AsciiKeyType(length: int) -> VARCHAR:  # noqa: N802
    """PK'ya giren ASCII kod alani (filing_type, action, board_code)."""
    return VARCHAR(length, charset="ascii", collation="ascii_bin")


def ProxyLabelType() -> VARCHAR:  # noqa: N802
    """proxies.label ve sync_run_items.proxy_label.

    UNIQUE anahtar oldugu icin ascii_bin: 'eu-1' != 'EU-1' olmali.
    """
    return VARCHAR(64, charset="ascii", collation="ascii_bin")


def HostType() -> VARCHAR:  # noqa: N802
    """Proxy hostname veya IP.

    Hostname'ler buyuk/kucuk harf duyarsizdir (RFC 4343), bu yuzden
    ascii_general_ci; tekillik kisiti da bu semantigi kullanir.
    """
    return VARCHAR(255, charset="ascii", collation="ascii_general_ci")


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
    (TaxRateForCalcs) bulunur; DECIMAL(38,0) oranlari yok ederdi.
    MySQL 11. basamagi SESSIZCE yuvarlar (yalnizca Note 1265), bu yuzden
    yuvarlama Python tarafinda quantize ile bilincli yapilir.
    """
    return Numeric(FACT_PRECISION, FACT_SCALE, asdecimal=True)


def BigNumType() -> Numeric[Decimal]:  # noqa: N802
    """marketCap / totalRevenue / enterpriseValue gibi buyuk degerler.
    'Tum sayisal info alanlari DECIMAL(28,12)' kestirmesi ERROR 1264 verir."""
    return Numeric(BIG_PRECISION, 0, asdecimal=True)


def TsType() -> DATETIME:  # noqa: N802
    """Tum zaman damgalari DATETIME(6).

    DATETIME(0) ayni saniyede PK cakismasi uretir (ERROR 1062) ve
    kesirleri yuvarlar (10:00:00.75 -> 10:00:01).
    """
    return DATETIME(fsp=6)


def RawJsonType() -> LONGTEXT:  # noqa: N802
    """raw_json LONGTEXT'tir, JSON degil (S5.4).

    JSON tipi anahtar sirasini degistirir (hash yeniden hesaplanamaz),
    NaN iceren govdeyi reddeder (ERROR 3140) ve float'i DOUBLE'a dusurur
    (0.001870 -> 0.00187). LONGTEXT byte-for-byte sadiktir.
    """
    return LONGTEXT()


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
