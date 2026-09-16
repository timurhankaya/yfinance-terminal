"""Ownership and insider tables.

Four of the five tables are as-of (the source returns the current roster);
insider_transactions carries its own date."""

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
    """majorHoldersBreakdown: a fixed set of four keys."""

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

    Both datasets write here; their scopes separate via
    `scope_columns=(symbol, as_of_date, holder_type)`."""

    __tablename__ = "institutional_holders"
    __table_args__ = (
        Index("ix_institutional_holders_holder", "holder"),
        Index("ix_institutional_holders_reported", "date_reported"),
        Index("ix_institutional_holders_as_of", "as_of_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    holder_type: Mapped[HolderType] = mapped_column(HOLDER_TYPE_ENUM, primary_key=True)
    holder: Mapped[str] = mapped_column(KeyTextType(128), primary_key=True)
    # Varies per row. Nullable: a single NaT would roll back the whole
    # symbol's transaction.
    date_reported: Mapped[date | None] = mapped_column(Date)
    pct_held: Mapped[Decimal | None] = mapped_column(PriceType())
    pct_change: Mapped[Decimal | None] = mapped_column(PriceType())
    shares: Mapped[Decimal | None] = mapped_column(BigNumType())
    value: Mapped[Decimal | None] = mapped_column(BigNumType())
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class InsiderActivity(Base):
    """netSharePurchaseActivity's 7-row presentation pivots into one row.

    Column 0's name is dynamic (it carries the period), so the row label
    is read by position and the period suffix is split into period_label."""

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
    # Signed: a non-negative constraint would drop the whole symbol on the
    # first negative value.
    purchases_trans: Mapped[int | None] = mapped_column(Integer)
    sales_trans: Mapped[int | None] = mapped_column(Integer)
    net_trans: Mapped[int | None] = mapped_column(Integer)
    net_pct: Mapped[Decimal | None] = mapped_column(PriceType())
    buy_pct: Mapped[Decimal | None] = mapped_column(PriceType())
    sell_pct: Mapped[Decimal | None] = mapped_column(PriceType())
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class InsiderTransaction(Base):
    """Insider transactions. Not as-of: the source carries a transaction date.

    The source can return exact duplicate rows (identical fact_hash), so
    normalize deduplicates first or the read/write count check would fail."""

    __tablename__ = "insider_transactions"
    __table_args__ = (
        Index("ix_insider_transactions_start", "start_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    start_date: Mapped[date] = mapped_column(Date, primary_key=True)
    fact_hash: Mapped[str] = mapped_column(ShortHashType(), primary_key=True)
    # Not always a person name; can be an institution.
    insider: Mapped[str | None] = mapped_column(PersonNameType())
    # '' maps to NULL.
    position: Mapped[str | None] = mapped_column(KeyTextType(64))
    text: Mapped[str | None] = mapped_column(String(255, collation="C"))
    # Usually '' -> NULL; kept so a future non-empty value needs no migration.
    transaction_label: Mapped[str | None] = mapped_column(String(64, collation="C"))
    url: Mapped[str | None] = mapped_column(Text)
    shares: Mapped[Decimal | None] = mapped_column(BigNumType())
    value: Mapped[Decimal | None] = mapped_column(BigNumType())
    # 'D', 'I', or 'D/I'.
    ownership: Mapped[str | None] = mapped_column(AsciiKeyType(8))
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class InsiderRosterHolder(Base):
    """Current insider roster.

    Source column set and order vary by symbol, so normalize uses
    row.get(...). positionSummary / positionSummaryDate can be a person's
    only share data, so the columns are kept."""

    __tablename__ = "insider_roster"
    __table_args__ = (
        Index("ix_insider_roster_as_of", "as_of_date"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    name: Mapped[str] = mapped_column(PersonNameType(), primary_key=True)
    position: Mapped[str | None] = mapped_column(KeyTextType(64))
    url: Mapped[str | None] = mapped_column(Text)
    most_recent_transaction: Mapped[str | None] = mapped_column(String(64, collation="C"))
    # Can arrive as datetime64 or raw epoch float64; kinds.py::_to_datetime
    # accepts both forms.
    latest_transaction_date: Mapped[datetime | None] = mapped_column(TsType())
    position_direct_date: Mapped[datetime | None] = mapped_column(TsType())
    position_indirect_date: Mapped[datetime | None] = mapped_column(TsType())
    shares_owned_directly: Mapped[Decimal | None] = mapped_column(BigNumType())
    shares_owned_indirectly: Mapped[Decimal | None] = mapped_column(BigNumType())
    position_summary: Mapped[Decimal | None] = mapped_column(BigNumType())
    position_summary_date: Mapped[datetime | None] = mapped_column(TsType())
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
