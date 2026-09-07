"""Fund content tables.

Hybrid schema: fields with a fixed shape get typed columns; percentages
with variable keys go to EAV. Measured basis: an equity fund has 11
sectors + 1 rating, a bond fund has 0 sectors + 9 ratings (BND, TLT,
AGG) -- no fixed column set covers both fund types.

`asset_classes` is not EAV: its 6 keys measured fixed across all 10
sample funds, so it is a typed column set on fund_profile.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Date, Enum, Index, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import (
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
    """Fund identity + operations + asset allocation, in one row."""

    __tablename__ = "fund_profile"
    __table_args__ = (
        Index("ix_fund_profile_as_of", "as_of_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    quote_type: Mapped[str] = mapped_column(AsciiKeyType(16), nullable=False)
    category_name: Mapped[str | None] = mapped_column(String(64, collation="C"))
    family: Mapped[str | None] = mapped_column(String(128, collation="C"))
    # Measured None for VFIAX/FCNTX.
    legal_type: Mapped[str | None] = mapped_column(String(64, collation="C"))
    # Measured max 555 chars (ARKK).
    description: Mapped[str | None] = mapped_column(Text)
    # fund_operations column 0 -- its name IS the symbol, read by position.
    expense_ratio: Mapped[Decimal | None] = mapped_column(PriceType())
    holdings_turnover: Mapped[Decimal | None] = mapped_column(PriceType())
    total_net_assets: Mapped[Decimal | None] = mapped_column(PriceType())
    expense_ratio_cat: Mapped[Decimal | None] = mapped_column(PriceType())
    holdings_turnover_cat: Mapped[Decimal | None] = mapped_column(PriceType())
    total_net_assets_cat: Mapped[Decimal | None] = mapped_column(PriceType())
    # asset_classes: same 6 keys in all 10 sample funds.
    cash_position: Mapped[Decimal | None] = mapped_column(PriceType())
    stock_position: Mapped[Decimal | None] = mapped_column(PriceType())
    bond_position: Mapped[Decimal | None] = mapped_column(PriceType())
    preferred_position: Mapped[Decimal | None] = mapped_column(PriceType())
    convertible_position: Mapped[Decimal | None] = mapped_column(PriceType())
    other_position: Mapped[Decimal | None] = mapped_column(PriceType())
    # Canonical body of eight sub-structures; preserves what would be lost
    # reducing to EAV. Measured row size ~1325 bytes (2% of budget).
    raw_json: Mapped[str] = mapped_column(RawJsonType(), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class FundMetric(Base):
    """equity_holdings + bond_holdings averages.

    `section` is part of the PK. Without it, the same `metric` name
    appearing in both sections would fail to write the second row:
      uniqueness violation: 'SPY-2026-09-04-price_to_earnings'
    Today's 9 names do not collide, but that is only Yahoo's naming
    choice -- the sibling table fund_weightings already puts `category`
    in its PK for the same reason.
    """

    __tablename__ = "fund_metrics"
    __table_args__ = (
        Index("ix_fund_metrics_as_of", "as_of_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    section: Mapped[FundSection] = mapped_column(SECTION_ENUM, primary_key=True)
    metric: Mapped[str] = mapped_column(AsciiKeyType(32), primary_key=True)
    value: Mapped[Decimal | None] = mapped_column(FactValueType())
    category_average: Mapped[Decimal | None] = mapped_column(FactValueType())
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class FundWeighting(Base):
    """sector_weightings + bond_ratings; the key set varies by fund type."""

    __tablename__ = "fund_weightings"
    __table_args__ = (
        Index("ix_fund_weightings_as_of", "as_of_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    category: Mapped[WeightCategory] = mapped_column(WEIGHT_CATEGORY_ENUM, primary_key=True)
    item_key: Mapped[str] = mapped_column(AsciiKeyType(32), primary_key=True)
    # Measured populated in all 10 sample funds.
    weight: Mapped[Decimal] = mapped_column(PriceType(), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class FundTopHolding(Base):
    """Fund -> constituent symbol relationship.

    `holding_symbol` has no FK: the source returns symbols outside the
    universe (BRK-B, 2330.TW, 005930.KQ, 0700.HK, even fund symbols like
    VRTPX, BISXX). An FK would roll back a fund's entire row set over one
    foreign symbol -- same reasoning as news_symbols. `is_known` marks the
    membership; an explicit index on (holding_symbol) exists because
    PostgreSQL creates none on its own, and the column is the last
    component of the PK so it cannot be searched alone.
    """

    __tablename__ = "fund_top_holdings"
    __table_args__ = (
        # PostgreSQL creates no index for this column on its own.
        Index("ix_fund_top_holdings_holding", "holding_symbol"),
        Index("ix_fund_top_holdings_as_of", "as_of_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    holding_symbol: Mapped[str] = mapped_column(SymbolType(), primary_key=True)
    # Measured max 51 chars (ARKK).
    holding_name: Mapped[str | None] = mapped_column(KeyTextType(128))
    holding_percent: Mapped[Decimal | None] = mapped_column(PriceType())
    # Not `rank`: it is a window function in PostgreSQL (and was reserved
    # in MySQL 8, where the guard originated). Source order is the data
    # itself (the "top 10" ranking).
    holding_rank: Mapped[int] = mapped_column(
        SmallInteger,
        CheckConstraint(
            '"holding_rank" BETWEEN 0 AND 255', name="ck_fund_top_holdings_holding_rank_range"
        ),
        nullable=False,
    )
    is_known: Mapped[bool] = mapped_column(Boolean, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
