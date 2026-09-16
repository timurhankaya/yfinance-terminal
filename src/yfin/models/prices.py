"""Price and corporate-action time series."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Index, text
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import (
    Base,
    PriceType,
    TsType,
    symbol_fk_column,
)


class PriceHistory(Base):
    """interval='1d'. PK is the exchange's local session date."""

    __tablename__ = "price_history"
    __table_args__ = (Index("ix_price_history_session_date", "session_date"),)

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    session_date: Mapped[date] = mapped_column(primary_key=True)
    # ts_utc preserves the raw value: converting to UTC on exchanges with a
    # positive offset would shift the date back a day.
    ts_utc: Mapped[datetime] = mapped_column(TsType(), nullable=False)

    open: Mapped[Decimal | None] = mapped_column(PriceType())
    high: Mapped[Decimal | None] = mapped_column(PriceType())
    low: Mapped[Decimal | None] = mapped_column(PriceType())
    close: Mapped[Decimal] = mapped_column(PriceType(), nullable=False)
    # Separate column returned when auto_adjust=False.
    adj_close: Mapped[Decimal | None] = mapped_column(PriceType())
    volume: Mapped[int | None] = mapped_column(
        BigInteger, CheckConstraint('"volume" >= 0', name="ck_price_history_volume_nonneg")
    )

    # These three columns are derived, not authoritative; the single source
    # of truth is dividends/splits/capital_gains, and v_actions reads only those.
    dividend: Mapped[Decimal] = mapped_column(PriceType(), nullable=False, server_default="0")
    split_ratio: Mapped[Decimal] = mapped_column(PriceType(), nullable=False, server_default="0")
    capital_gain: Mapped[Decimal] = mapped_column(PriceType(), nullable=False, server_default="0")

    # yfinance history(repair=True)'s "Repaired?" column. Monotonic:
    # only advances 0 -> 1. Repair heuristics depend on window length, so
    # a narrow incremental window can see the same row as 1 then later as
    # 0; a plain upsert would write that back and reset the audit value.
    is_repaired: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class Dividend(Base):
    __tablename__ = "dividends"
    __table_args__ = (Index("ix_dividends_ex_date", "ex_date"),)

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    ex_date: Mapped[date] = mapped_column(primary_key=True)
    amount: Mapped[Decimal] = mapped_column(PriceType(), nullable=False)


class Split(Base):
    __tablename__ = "splits"
    __table_args__ = (Index("ix_splits_split_date", "split_date"),)

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    split_date: Mapped[date] = mapped_column(primary_key=True)
    ratio: Mapped[Decimal] = mapped_column(PriceType(), nullable=False)


class CapitalGain(Base):
    __tablename__ = "capital_gains"
    __table_args__ = (Index("ix_capital_gains_gain_date", "gain_date"),)

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    gain_date: Mapped[date] = mapped_column(primary_key=True)
    amount: Mapped[Decimal] = mapped_column(PriceType(), nullable=False)


class SharesFull(Base):
    """'shares' is part of the PK: the source returns different values for
    the same date, and with (symbol, as_of_date) alone which value wins
    would depend on run order."""

    __tablename__ = "shares_full"
    __table_args__ = (Index("ix_shares_full_as_of_date", "as_of_date"),)

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(primary_key=True)
    shares: Mapped[int] = mapped_column(
        BigInteger,
        CheckConstraint('"shares" >= 0', name="ck_shares_full_shares_nonneg"),
        primary_key=True,
    )
    ts_utc: Mapped[datetime | None] = mapped_column(TsType())
