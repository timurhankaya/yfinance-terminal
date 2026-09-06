"""Fon icerik tablolari (AH S5.3).

Hibrit sema: sabit olculen alanlar tipli kolona, DEGISKEN anahtarli yuzdeler
EAV'a gider. Gerekce olcumdur: hisse fonunda 11 sektor + 1 rating, tahvil
fonunda 0 sektor + 9 rating (BND, TLT, AGG). Sabit kolon seti iki fon tipini
birden tasiyamaz.

`asset_classes` EAV'a GIRMEZ: 6 anahtari 10 fonun 10'unda da sabit olculdu,
bu yuzden fund_profile'da tipli kolondur.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, Enum, Index, String, Text
from sqlalchemy.dialects.mysql import TINYINT
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import (
    MYSQL_TABLE_ARGS,
    AsciiKeyType,
    Base,
    FactValueType,
    KeyTextType,
    PriceType,
    RawJsonType,
    SymbolType,
    TsType,
    symbol_fk_column,
)


class FundSection(enum.StrEnum):
    EQUITY = "equity"
    BOND = "bond"


class WeightCategory(enum.StrEnum):
    SECTOR = "sector"
    BOND_RATING = "bond_rating"


SECTION_ENUM = Enum(
    FundSection,
    values_callable=lambda e: [m.value for m in e],
    name="fund_section",
    native_enum=True,
)
WEIGHT_CATEGORY_ENUM = Enum(
    WeightCategory,
    values_callable=lambda e: [m.value for m in e],
    name="fund_weight_category",
    native_enum=True,
)


class FundProfile(Base):
    """Fon kimligi + operasyon + varlik dagilimi, tek satirda."""

    __tablename__ = "fund_profile"
    __table_args__ = (
        Index("ix_fund_profile_as_of", "as_of_date"),
        MYSQL_TABLE_ARGS,
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    quote_type: Mapped[str] = mapped_column(AsciiKeyType(16), nullable=False)
    category_name: Mapped[str | None] = mapped_column(String(64))
    family: Mapped[str | None] = mapped_column(String(128))
    # VFIAX/FCNTX'te None olculdu
    legal_type: Mapped[str | None] = mapped_column(String(64))
    # Olculen max 555 (ARKK)
    description: Mapped[str | None] = mapped_column(Text)
    # fund_operations 0. kolonu -- ADI SEMBOLUN KENDISIDIR, konumdan okunur
    expense_ratio: Mapped[Decimal | None] = mapped_column(PriceType())
    holdings_turnover: Mapped[Decimal | None] = mapped_column(PriceType())
    total_net_assets: Mapped[Decimal | None] = mapped_column(PriceType())
    expense_ratio_cat: Mapped[Decimal | None] = mapped_column(PriceType())
    holdings_turnover_cat: Mapped[Decimal | None] = mapped_column(PriceType())
    total_net_assets_cat: Mapped[Decimal | None] = mapped_column(PriceType())
    # asset_classes: 10/10 fonda ayni 6 anahtar
    cash_position: Mapped[Decimal | None] = mapped_column(PriceType())
    stock_position: Mapped[Decimal | None] = mapped_column(PriceType())
    bond_position: Mapped[Decimal | None] = mapped_column(PriceType())
    preferred_position: Mapped[Decimal | None] = mapped_column(PriceType())
    convertible_position: Mapped[Decimal | None] = mapped_column(PriceType())
    other_position: Mapped[Decimal | None] = mapped_column(PriceType())
    # Sekiz alt yapinin kanonik govdesi; EAV'a indirgenirken kaybolabilecek
    # bilgiyi korur. Olculen satir boyutu ~1325 byte (butcenin %2'si).
    raw_json: Mapped[str] = mapped_column(RawJsonType(), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class FundMetric(Base):
    """equity_holdings + bond_holdings ortalamalari.

    `section` PK'DADIR. Disarida birakilsaydi ayni `metric` adi iki bolumde
    geldiginde ikinci satir yazilamazdi:
      ERROR 1062: Duplicate entry 'SPY-2026-09-04-price_to_earnings'
    (gercek MySQL 8.3'te dogrulandi). Bugunku 9 ad cakismiyor ama bunu
    garanti eden sey yalnizca Yahoo'nun ad secimidir; kardes tablo
    fund_weightings zaten `category`'yi PK'ya koyuyor.
    """

    __tablename__ = "fund_metrics"
    __table_args__ = (
        Index("ix_fund_metrics_as_of", "as_of_date"),
        MYSQL_TABLE_ARGS,
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    section: Mapped[FundSection] = mapped_column(SECTION_ENUM, primary_key=True)
    metric: Mapped[str] = mapped_column(AsciiKeyType(32), primary_key=True)
    value: Mapped[Decimal | None] = mapped_column(FactValueType())
    category_average: Mapped[Decimal | None] = mapped_column(FactValueType())
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class FundWeighting(Base):
    """sector_weightings + bond_ratings; anahtar seti fon tipine gore degisir."""

    __tablename__ = "fund_weightings"
    __table_args__ = (
        Index("ix_fund_weightings_as_of", "as_of_date"),
        MYSQL_TABLE_ARGS,
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    category: Mapped[WeightCategory] = mapped_column(WEIGHT_CATEGORY_ENUM, primary_key=True)
    item_key: Mapped[str] = mapped_column(AsciiKeyType(32), primary_key=True)
    # 10 fonun hepsinde dolu olculdu
    weight: Mapped[Decimal] = mapped_column(PriceType(), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class FundTopHolding(Base):
    """Fon -> bilesen sembol iliskisi.

    "Relationlari sembol kodu uzerinden kur" bu tabloda karsilanir.
    `holding_symbol`'de FK YOKTUR: kaynakta evren disi semboller geliyor
    (BRK-B, 2330.TW, 005930.KQ, 0700.HK ve hatta FON sembolleri VRTPX,
    BISXX). FK olsaydi sembol basina tek transaction geregi FONUN TUM VERISI
    rollback olurdu -- news_symbols ile birebir ayni gerekce. `is_known`
    bagi isaretler; (holding_symbol) uzerinde ACIK indeks vardir cunku
    InnoDB FK'siz indeks acmaz VE kolon PK'nin SON bileseni oldugu icin tek
    basina aranamaz.
    """

    __tablename__ = "fund_top_holdings"
    __table_args__ = (
        # FK yok -> otomatik indeks yok; SHOW INDEX ile dogrulandi
        Index("ix_fund_top_holdings_holding", "holding_symbol"),
        Index("ix_fund_top_holdings_as_of", "as_of_date"),
        MYSQL_TABLE_ARGS,
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    holding_symbol: Mapped[str] = mapped_column(SymbolType(), primary_key=True)
    # Olculen max 51 (ARKK)
    holding_name: Mapped[str | None] = mapped_column(KeyTextType(128))
    holding_percent: Mapped[Decimal | None] = mapped_column(PriceType())
    # Ad `rank` OLAMAZ: MySQL 8'de window fonksiyonu olarak rezerve
    # (CREATE TABLE ... rank ... -> ERROR 1064). Kaynak sirasi verinin
    # kendisidir ("ilk 10" siralamasi).
    holding_rank: Mapped[int] = mapped_column(TINYINT(unsigned=True), nullable=False)
    is_known: Mapped[bool] = mapped_column(Boolean, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
