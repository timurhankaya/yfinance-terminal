"""Financial statements, calendar, earnings_dates, and SEC filings.

Financial statements use a long (EAV) schema: the item set varies by
symbol and sector (302 distinct labels measured across 10 symbols); a
wide schema would need a migration for every new item.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Column,
    Date,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Table,
    Text,
    desc,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import (
    AsciiKeyType,
    Base,
    BigNumType,
    FactValueType,
    HashType,
    PriceType,
    RawJsonType,
    ShortHashType,
    SymbolType,
    TsType,
)


class StatementKind(enum.StrEnum):
    # PostgreSQL stores ENUM values by `pg_enum` OID and links FKs by
    # label, so an FK survives a value's order changing (measured: after
    # `ALTER TYPE ... ADD VALUE ... BEFORE`, enumsortorder became 1.5 and
    # the composite-FK row stayed intact). No MySQL-style ordinal trap;
    # a value can be inserted in the middle.
    INCOME = "income"
    BALANCE_SHEET = "balance_sheet"
    CASH_FLOW = "cash_flow"
    # get_valuation_measures has the same shape as the financial
    # statements (index=item label, column=period end); rather than a
    # separate table, it enters as a fourth value on the EAV's
    # `statement` dimension.
    VALUATION = "valuation"


class StatementFreq(enum.StrEnum):
    ANNUAL = "annual"
    QUARTERLY = "quarterly"
    TTM = "ttm"


def _enum_values(e: type[enum.Enum]) -> list[str]:
    return [m.value for m in e]


# The ENUM definition comes from one source and is shared by two tables.
# The reasoning is single-definition discipline: two separate Enum()
# objects could drift apart silently. There is no technical collision
# risk -- SQLAlchemy deduplicates a same-named type within one MetaData
# even under checkfirst=False (measured); this is a maintenance
# decision, not a necessity.
STATEMENT_ENUM = Enum(
    StatementKind, values_callable=_enum_values, name="statement_kind", native_enum=True
)
FREQ_ENUM = Enum(
    StatementFreq, values_callable=_enum_values, name="statement_freq", native_enum=True
)

# yfinance's freq parameter ('yearly'/'quarterly'/'trailing') is
# deliberately different from the schema names; the mapping is defined
# here, in one place.
API_FREQ: dict[StatementFreq, str] = {
    StatementFreq.ANNUAL: "yearly",
    StatementFreq.QUARTERLY: "quarterly",
    StatementFreq.TTM: "trailing",
}

# Measured max item label 60 chars (MSFT balance sheet); the closed
# universe (const.fundamentals_keys, 375 labels) also maxes at 60. Extra
# VARCHAR width carries no storage cost.
ITEM_KEY_LENGTH = 128


class FinancialPeriod(Base):
    """A (symbol, statement, freq, period) header row.

    Items are not rewritten when content_hash is unchanged; the header
    row is still written, and fetched_at advances as "last verified time".
    """

    __tablename__ = "financial_periods"
    __table_args__ = (
        Index("ix_financial_periods_period_end", "period_end"),
    )

    symbol: Mapped[str] = mapped_column(
        SymbolType(),
        ForeignKey("symbols.symbol", onupdate="CASCADE", ondelete="RESTRICT"),
        primary_key=True,
    )
    statement: Mapped[StatementKind] = mapped_column(STATEMENT_ENUM, primary_key=True)
    freq: Mapped[StatementFreq] = mapped_column(FREQ_ENUM, primary_key=True)
    period_end: Mapped[date] = mapped_column(Date, primary_key=True)
    # info.financialCurrency; THYAO.IS statements are USD, prices TRY.
    currency: Mapped[str | None] = mapped_column(String(8, collation="C"))
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    raw_json: Mapped[str] = mapped_column(RawJsonType(), nullable=False)
    content_hash: Mapped[str] = mapped_column(HashType(), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class FinancialFact(Base):
    """A single item value.

    No direct FK to symbols: the constraint goes through the parent, and
    ON DELETE RESTRICT is enforced there. The composite FK columns are a
    prefix of the PK, so no extra index is needed.
    """

    __tablename__ = "financial_facts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["symbol", "statement", "freq", "period_end"],
            [
                "financial_periods.symbol",
                "financial_periods.statement",
                "financial_periods.freq",
                "financial_periods.period_end",
            ],
            onupdate="CASCADE",
            ondelete="CASCADE",
            name="fk_financial_facts_period",
        ),
        # Without this, WHERE period_end=? does a full scan (148ms on 600k rows).
        Index("ix_financial_facts_period_item", "period_end", "item_key"),
        # "TotalRevenue across all symbols".
        Index("ix_financial_facts_item_period", "item_key", "period_end"),
    )

    symbol: Mapped[str] = mapped_column(SymbolType(), primary_key=True)
    statement: Mapped[StatementKind] = mapped_column(STATEMENT_ENUM, primary_key=True)
    freq: Mapped[StatementFreq] = mapped_column(FREQ_ENUM, primary_key=True)
    period_end: Mapped[date] = mapped_column(Date, primary_key=True)
    item_key: Mapped[str] = mapped_column(AsciiKeyType(ITEM_KEY_LENGTH), primary_key=True)
    # NOT NULL since NaN cells are never written.
    value: Mapped[Decimal] = mapped_column(FactValueType(), nullable=False)


# --- ticker_calendar (snapshot pair) ---------------------------------------


def _calendar_columns() -> list[Column[Any]]:
    return [
        Column("dividend_date", Date, nullable=True),
        Column("ex_dividend_date", Date, nullable=True),
        Column("earnings_date_start", Date, nullable=True),
        Column("earnings_date_end", Date, nullable=True),
        # Raw list length: preserves the "single date" vs. "range" distinction.
        Column("earnings_date_count", SmallInteger, nullable=False, server_default="0"),
        Column("earnings_high", PriceType(), nullable=True),
        Column("earnings_low", PriceType(), nullable=True),
        Column("earnings_average", PriceType(), nullable=True),
        Column("revenue_high", BigNumType(), nullable=True),
        Column("revenue_low", BigNumType(), nullable=True),
        Column("revenue_average", BigNumType(), nullable=True),
        Column("raw_json", RawJsonType(), nullable=False),
        Column("content_hash", HashType(), nullable=False),
    ]


def _calendar_table(name: str, *, historical: bool) -> Table:
    cols: list[Column[Any]] = [
        Column(
            "symbol",
            SymbolType(),
            ForeignKey("symbols.symbol", onupdate="CASCADE", ondelete="RESTRICT"),
            primary_key=True,
            nullable=False,
        )
    ]
    if historical:
        cols.append(Column("fetched_at", TsType(), primary_key=True, nullable=False))
    cols.extend(_calendar_columns())
    if not historical:
        cols.append(Column("fetched_at", TsType(), nullable=False))
    # No (symbol, fetched_at DESC) index is added: it is exactly the PK,
    # and a PostgreSQL btree index can be scanned in either direction.
    return Table(name, Base.metadata, *cols)


ticker_calendar = _calendar_table("ticker_calendar", historical=False)
ticker_calendar_history = _calendar_table("ticker_calendar_history", historical=True)


class EarningsDate(Base):
    """Past and future earnings dates.

    fact_hash must be part of the PK: AAPL's 2002-07-16 16:00 timestamp
    has two rows differing only in Surprise(%) (2.55 / 13.43), with both
    EPS fields NaN. A (symbol, ts) PK would leave which row wins to run
    order.
    """

    __tablename__ = "earnings_dates"
    __table_args__ = (
        Index("ix_earnings_dates_ts", "earnings_ts_utc"),
    )

    symbol: Mapped[str] = mapped_column(
        SymbolType(),
        ForeignKey("symbols.symbol", onupdate="CASCADE", ondelete="RESTRICT"),
        primary_key=True,
    )
    earnings_ts_utc: Mapped[datetime] = mapped_column(TsType(), primary_key=True)
    fact_hash: Mapped[str] = mapped_column(ShortHashType(), primary_key=True)
    earnings_date_local: Mapped[date] = mapped_column(Date, nullable=False)
    # Measured America/New_York for all symbols, including THYAO.IS, SAP.DE, 7203.T.
    tz_name: Mapped[str] = mapped_column(String(64, collation="C"), nullable=False)
    eps_estimate: Mapped[Decimal | None] = mapped_column(PriceType())
    reported_eps: Mapped[Decimal | None] = mapped_column(PriceType())
    surprise_pct: Mapped[Decimal | None] = mapped_column(PriceType())
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class SecFiling(Base):
    """SEC filings. Outside the US the source returns {} (dict) -> empty."""

    __tablename__ = "sec_filings"
    __table_args__ = (
        Index("ix_sec_filings_symbol_date", "symbol", desc(text("filing_date"))),
        Index("ix_sec_filings_type", "filing_type"),
    )

    symbol: Mapped[str] = mapped_column(
        SymbolType(),
        ForeignKey("symbols.symbol", onupdate="CASCADE", ondelete="RESTRICT"),
        primary_key=True,
    )
    # The accession number from within edgarUrl (80/80 succeeded); falls
    # back to sha256(date|type|title)[:32] if not found.
    filing_id: Mapped[str] = mapped_column(AsciiKeyType(64), primary_key=True)
    filing_date: Mapped[date] = mapped_column(Date, nullable=False)
    filed_ts_utc: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    filing_type: Mapped[str] = mapped_column(AsciiKeyType(32), nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    edgar_url: Mapped[str | None] = mapped_column(Text)
    # yfinance converts the exhibit list to a {type: url} dict, so a
    # second exhibit of the same type overwrites the first; this counts
    # distinct exhibit types.
    exhibit_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    raw_json: Mapped[str] = mapped_column(RawJsonType(), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class SecFilingExhibit(Base):
    """Filing exhibits.

    url_hash is part of the PK: the same filing can have two EX-99.1
    exhibits with different URLs, and a (symbol, filing_id, exhibit_type)
    PK would drop the second one with a uniqueness violation. url is TEXT
    and cannot go into the PK directly (btree tuple size limit).
    """

    __tablename__ = "sec_filing_exhibits"
    __table_args__ = (
        ForeignKeyConstraint(
            ["symbol", "filing_id"],
            ["sec_filings.symbol", "sec_filings.filing_id"],
            onupdate="CASCADE",
            ondelete="CASCADE",
            name="fk_sec_filing_exhibits_filing",
        ),
    )

    symbol: Mapped[str] = mapped_column(SymbolType(), primary_key=True)
    filing_id: Mapped[str] = mapped_column(AsciiKeyType(64), primary_key=True)
    exhibit_type: Mapped[str] = mapped_column(AsciiKeyType(32), primary_key=True)
    url_hash: Mapped[str] = mapped_column(ShortHashType(), primary_key=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)


__all__ = [
    "API_FREQ",
    "FREQ_ENUM",
    "ITEM_KEY_LENGTH",
    "STATEMENT_ENUM",
    "EarningsDate",
    "FinancialFact",
    "FinancialPeriod",
    "SecFiling",
    "SecFilingExhibit",
    "StatementFreq",
    "StatementKind",
    "ticker_calendar",
    "ticker_calendar_history",
]
