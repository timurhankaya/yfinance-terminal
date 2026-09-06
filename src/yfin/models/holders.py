"""Ownership and insider tables.

Four of the five tables are as-of (the source returns "current top 10 /
current roster"). insider_transactions is not as-of, since the source
carries its own date.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, Enum, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import (
    AsciiKeyType,
    Base,
    BigNumType,
    KeyTextType,
    PersonNameType,
    PriceType,
    ShortHashType,
    TsType,
    symbol_fk_column,
)


class HolderType(enum.StrEnum):
    INSTITUTION = "institution"
    MUTUALFUND = "mutualfund"


HOLDER_TYPE_ENUM = Enum(
    HolderType,
    values_callable=lambda e: [m.value for m in e],
    name="holder_type",
    native_enum=True,
)


class HolderBreakdown(Base):
    """majorHoldersBreakdown: same 4 keys measured in 19/19 symbols."""

    __tablename__ = "holder_breakdown"
    __table_args__ = (
        Index("ix_holder_breakdown_as_of", "as_of_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    insiders_pct_held: Mapped[Decimal | None] = mapped_column(PriceType())
    institutions_pct_held: Mapped[Decimal | None] = mapped_column(PriceType())
    institutions_float_pct_held: Mapped[Decimal | None] = mapped_column(PriceType())
    # Source returns a float (7750.0).
    institutions_count: Mapped[int | None] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class InstitutionalHolder(Base):
    """institutional_holders + mutualfund_holders in one table.

    Column sets measured identical across 14 symbols. Both datasets write
    to this table; their scopes separate via
    `scope_columns=(symbol, as_of_date, holder_type)` -- this is exactly
    why scope_columns exists.
    """

    __tablename__ = "institutional_holders"
    __table_args__ = (
        Index("ix_institutional_holders_holder", "holder"),
        Index("ix_institutional_holders_reported", "date_reported"),
        Index("ix_institutional_holders_as_of", "as_of_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    holder_type: Mapped[HolderType] = mapped_column(HOLDER_TYPE_ENUM, primary_key=True)
    # Measured max 70 chars (JPM mutualfund). PK total 548 bytes (measured in MySQL).
    holder: Mapped[str] = mapped_column(KeyTextType(128), primary_key=True)
    # Varies per row: AAPL mutualfund has 4 different dates in one list.
    # Nullable: not measured to always be populated, and a single NaT
    # would roll back the whole symbol's transaction.
    date_reported: Mapped[date | None] = mapped_column(Date)
    pct_held: Mapped[Decimal | None] = mapped_column(PriceType())
    pct_change: Mapped[Decimal | None] = mapped_column(PriceType())
    # Measured max: shares 1.94e9, value 1.76e13.
    shares: Mapped[Decimal | None] = mapped_column(BigNumType())
    value: Mapped[Decimal | None] = mapped_column(BigNumType())
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class InsiderActivity(Base):
    """netSharePurchaseActivity's 7-row presentation pivots into one row.

    The source is already a 7-row display of a single record; column 0's
    name is dynamic ('Insider Purchases Last 6m'), so the row label is
    read by position, not name, and the period suffix is split into
    period_label.
    """

    __tablename__ = "insider_activity"
    __table_args__ = (
        Index("ix_insider_activity_as_of", "as_of_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    period_label: Mapped[str] = mapped_column(AsciiKeyType(8), nullable=False)
    # Can be negative (KO net -547_806) -> signed DECIMAL(38,0).
    purchases_shares: Mapped[Decimal | None] = mapped_column(BigNumType())
    sales_shares: Mapped[Decimal | None] = mapped_column(BigNumType())
    net_shares: Mapped[Decimal | None] = mapped_column(BigNumType())
    total_insider_shares: Mapped[Decimal | None] = mapped_column(BigNumType())
    # Signed Integer: net transaction count can be negative in principle
    # and this was not measured out; a non-negative constraint would drop
    # the whole symbol on the first negative value.
    purchases_trans: Mapped[int | None] = mapped_column(Integer)
    sales_trans: Mapped[int | None] = mapped_column(Integer)
    net_trans: Mapped[int | None] = mapped_column(Integer)
    net_pct: Mapped[Decimal | None] = mapped_column(PriceType())
    buy_pct: Mapped[Decimal | None] = mapped_column(PriceType())
    sell_pct: Mapped[Decimal | None] = mapped_column(PriceType())
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class InsiderTransaction(Base):
    """Insider transactions. Not as-of: the source carries a transaction date.

    fact_hash is part of the PK but not sufficient alone: PFE measured two
    rows identical across all nine columns (BOSHOFF CHRISTOFFEL, 8741
    shares, value 263716, 2025-02-21), with identical hashes too. So
    normalize deduplicates exact duplicates first; otherwise 34 rows read
    would write 33, breaking the verification check on every run.
    """

    __tablename__ = "insider_transactions"
    __table_args__ = (
        Index("ix_insider_transactions_start", "start_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    start_date: Mapped[date] = mapped_column(Date, primary_key=True)
    fact_hash: Mapped[str] = mapped_column(ShortHashType(), primary_key=True)
    # Not always a person name: 'Elliott Investment Management L.P' (BP.L).
    # Measured max 33 chars.
    insider: Mapped[str | None] = mapped_column(PersonNameType())
    # Measured max 56 (WMT); '' maps to NULL (measured empty for BP.L).
    position: Mapped[str | None] = mapped_column(KeyTextType(64))
    text: Mapped[str | None] = mapped_column(String(255, collation="C"))
    # All 1464 rows / 16 symbols measured '' -> NULL. Column kept anyway so
    # a future non-empty value needs no migration.
    transaction_label: Mapped[str | None] = mapped_column(String(64, collation="C"))
    url: Mapped[str | None] = mapped_column(Text)
    shares: Mapped[Decimal | None] = mapped_column(BigNumType())
    # NaN in all rows measured for DIS and BP.L.
    value: Mapped[Decimal | None] = mapped_column(BigNumType())
    # 'D', 'I', and 'D/I' (XOM) -> VARCHAR(2) was too narrow.
    ownership: Mapped[str | None] = mapped_column(AsciiKeyType(8))
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class InsiderRosterHolder(Base):
    """Current insider roster (9-10 people).

    Source column set is 7/9/11 depending on symbol, and order is not
    fixed either -> normalize uses row.get(...). positionSummary /
    positionSummaryDate were seen only for NVDA, where they were a
    person's only share data; omitting the column would leave every
    share field NULL for that row.
    """

    __tablename__ = "insider_roster"
    __table_args__ = (
        Index("ix_insider_roster_as_of", "as_of_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    # PK total 1055 bytes (measured in MySQL, limit 3072).
    name: Mapped[str] = mapped_column(PersonNameType(), primary_key=True)
    position: Mapped[str | None] = mapped_column(KeyTextType(64))
    url: Mapped[str | None] = mapped_column(Text)
    most_recent_transaction: Mapped[str | None] = mapped_column(String(64, collation="C"))
    # Can arrive as datetime64 or raw epoch float64 (measured populated
    # float in 6 symbols); kinds.py::_to_datetime accepts both forms.
    latest_transaction_date: Mapped[datetime | None] = mapped_column(TsType())
    position_direct_date: Mapped[datetime | None] = mapped_column(TsType())
    position_indirect_date: Mapped[datetime | None] = mapped_column(TsType())
    shares_owned_directly: Mapped[Decimal | None] = mapped_column(BigNumType())
    shares_owned_indirectly: Mapped[Decimal | None] = mapped_column(BigNumType())
    position_summary: Mapped[Decimal | None] = mapped_column(BigNumType())
    position_summary_date: Mapped[datetime | None] = mapped_column(TsType())
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
