"""Analyst tables.

Six of the eight tables are as-of: the source returns only "now", and the
period label is relative (0q, +1y, 0m, -1m) -- data is meaningless
without `as_of_date`. Two (analyst_grade_changes, earnings_history) are
not as-of, since the source carries its own date.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Date,
    Enum,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import (
    AsciiKeyType,
    Base,
    FactValueType,
    KeyTextType,
    PriceType,
    TsType,
    symbol_fk_column,
)

# Relative period label: '0q', '+1q', '0y', '+1y', 'LTG' / '0m'..'-3m'
PERIOD_LENGTH = 8


class EstimateMetric(enum.StrEnum):
    EPS = "eps"
    REVENUE = "revenue"


# Single source for the enum definition. PostgreSQL enum order is not
# ordinal (the pg_enum OID is stored), so it lacks MySQL's silent-wrong-
# value trap; a single source is still kept for maintainability.
METRIC_ENUM = Enum(
    EstimateMetric,
    values_callable=lambda e: [m.value for m in e],
    name="estimate_metric",
    native_enum=True,
)


class AnalystRecommendation(Base):
    """strongBuy..strongSell counters; row count measured as 3 or 4."""

    __tablename__ = "analyst_recommendations"
    __table_args__ = (
        Index("ix_analyst_recommendations_as_of", "as_of_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    period: Mapped[str] = mapped_column(AsciiKeyType(PERIOD_LENGTH), primary_key=True)
    # int64 with no NaN in all 19 symbols -> NOT NULL is defensible.
    strong_buy: Mapped[int] = mapped_column(Integer, nullable=False)
    buy: Mapped[int] = mapped_column(Integer, nullable=False)
    hold: Mapped[int] = mapped_column(Integer, nullable=False)
    sell: Mapped[int] = mapped_column(Integer, nullable=False)
    strong_sell: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class AnalystGradeChange(Base):
    """Analyst grade changes. Not as-of: the source carries its own date.

    Pure upsert. With `replace_scope`, older records past the ~1000-row
    cap would be deleted on every run.
    """

    __tablename__ = "analyst_grade_changes"
    __table_args__ = (
        Index("ix_analyst_grade_changes_ts", "grade_ts_utc"),
        Index("ix_analyst_grade_changes_firm", "firm"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    # Source is tz-naive but derived from epochGradeDate in seconds -> UTC;
    # no second tz conversion is applied.
    grade_ts_utc: Mapped[datetime] = mapped_column(TsType(), primary_key=True)
    # (GradeDate, Firm) measured dup=0 across 15 symbols / 8852 rows; max 26 chars.
    firm: Mapped[str] = mapped_column(KeyTextType(64), primary_key=True)
    to_grade: Mapped[str | None] = mapped_column(String(32, collation="C"))
    from_grade: Mapped[str | None] = mapped_column(String(32, collation="C"))
    # Not an ENUM: 5 values measured, which does not prove Yahoo's list is closed.
    action: Mapped[str | None] = mapped_column(AsciiKeyType(16))
    price_target_action: Mapped[str | None] = mapped_column(String(16, collation="C"))
    # 0.0 is a real value ("no target"), not converted to NULL.
    current_price_target: Mapped[Decimal | None] = mapped_column(PriceType())
    prior_price_target: Mapped[Decimal | None] = mapped_column(PriceType())
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class AnalystPriceTarget(Base):
    """current/low/high/mean/median. No consistency constraint: THYAO
    measured low(330) > current(294); the source value is written as-is."""

    __tablename__ = "analyst_price_targets"
    __table_args__ = (
        Index("ix_analyst_price_targets_as_of", "as_of_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    current: Mapped[Decimal | None] = mapped_column(PriceType())
    low: Mapped[Decimal | None] = mapped_column(PriceType())
    high: Mapped[Decimal | None] = mapped_column(PriceType())
    mean: Mapped[Decimal | None] = mapped_column(PriceType())
    median: Mapped[Decimal | None] = mapped_column(PriceType())
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class AnalystEstimate(Base):
    """earnings_estimate + revenue_estimate in one table; identical column
    sets, both from a single module.

    FactValueType (DECIMAL(38,10)) is required: the same column holds
    AAPL EPS 1.97656 and THYAO revenue 1_285_436_390_920.
    """

    __tablename__ = "analyst_estimates"
    __table_args__ = (
        Index("ix_analyst_estimates_as_of", "as_of_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    metric: Mapped[EstimateMetric] = mapped_column(METRIC_ENUM, primary_key=True)
    period: Mapped[str] = mapped_column(AsciiKeyType(PERIOD_LENGTH), primary_key=True)
    avg: Mapped[Decimal | None] = mapped_column(FactValueType())
    low: Mapped[Decimal | None] = mapped_column(FactValueType())
    high: Mapped[Decimal | None] = mapped_column(FactValueType())
    year_ago_value: Mapped[Decimal | None] = mapped_column(FactValueType())
    # Source can return a float (1.0) or NaN.
    number_of_analysts: Mapped[int | None] = mapped_column(Integer)
    growth: Mapped[Decimal | None] = mapped_column(PriceType())
    # Column added by yfinance; undocumented.
    currency: Mapped[str | None] = mapped_column(AsciiKeyType(8))
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class AnalystEpsTrend(Base):
    """EPS estimate trend over time. A column name cannot start with a
    digit: 7daysAgo -> days_ago_7."""

    __tablename__ = "analyst_eps_trend"
    __table_args__ = (
        Index("ix_analyst_eps_trend_as_of", "as_of_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    period: Mapped[str] = mapped_column(AsciiKeyType(PERIOD_LENGTH), primary_key=True)
    current: Mapped[Decimal | None] = mapped_column(FactValueType())
    days_ago_7: Mapped[Decimal | None] = mapped_column(FactValueType())
    days_ago_30: Mapped[Decimal | None] = mapped_column(FactValueType())
    days_ago_60: Mapped[Decimal | None] = mapped_column(FactValueType())
    days_ago_90: Mapped[Decimal | None] = mapped_column(FactValueType())
    currency: Mapped[str | None] = mapped_column(AsciiKeyType(8))
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class AnalystEpsRevision(Base):
    """Up/down revision counters.

    Source keys: upLast7days, upLast30days, downLast30days, and
    downLast7Days -- the last one has a capital D (confirmed 19/19
    symbols). Documentation shows all four lowercase; reading with a
    lowercase `d` leaves the column silently NULL forever.
    """

    __tablename__ = "analyst_eps_revisions"
    __table_args__ = (
        Index("ix_analyst_eps_revisions_as_of", "as_of_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    period: Mapped[str] = mapped_column(AsciiKeyType(PERIOD_LENGTH), primary_key=True)
    up_last_7d: Mapped[int | None] = mapped_column(Integer)
    up_last_30d: Mapped[int | None] = mapped_column(Integer)
    down_last_7d: Mapped[int | None] = mapped_column(Integer)
    down_last_30d: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str | None] = mapped_column(AsciiKeyType(8))
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class AnalystGrowthEstimate(Base):
    """Growth estimates; period 0q/+1q/0y/+1y/LTG.

    industry_trend and sector_trend never appeared in 19 sample symbols,
    but yfinance explicitly requests them (industryTrend, sectorTrend,
    indexTrend); the columns exist now so a future value needs no migration.

    index_trend is identical across all symbols (the market index trend);
    storing it denormalized per symbol is deliberate -- a separate market
    table for one column would be an abstraction with no other use.
    """

    __tablename__ = "analyst_growth_estimates"
    __table_args__ = (
        Index("ix_analyst_growth_estimates_as_of", "as_of_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    period: Mapped[str] = mapped_column(AsciiKeyType(PERIOD_LENGTH), primary_key=True)
    stock_trend: Mapped[Decimal | None] = mapped_column(PriceType())
    index_trend: Mapped[Decimal | None] = mapped_column(PriceType())
    industry_trend: Mapped[Decimal | None] = mapped_column(PriceType())
    sector_trend: Mapped[Decimal | None] = mapped_column(PriceType())
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class EarningsHistoryRow(Base):
    """Actual vs. estimated EPS. Not as-of: the source gives quarter-end.

    quarter_end is taken from a tz-naive Timestamp via .date(); no tz
    conversion is applied -- a fiscal quarter is a calendar label, not an
    instant.
    """

    __tablename__ = "earnings_history"
    __table_args__ = (
        Index("ix_earnings_history_quarter", "quarter_end"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    quarter_end: Mapped[date] = mapped_column(Date, primary_key=True)
    eps_actual: Mapped[Decimal | None] = mapped_column(PriceType())
    eps_estimate: Mapped[Decimal | None] = mapped_column(PriceType())
    eps_difference: Mapped[Decimal | None] = mapped_column(PriceType())
    surprise_percent: Mapped[Decimal | None] = mapped_column(PriceType())
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
