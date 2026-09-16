"""Option expirations and chains.

`option_expirations` records which expiries existed on a day; `option_quotes`
holds chains for only the first few (`yf_option_expiries`), so the list is
not a summary of the quotes. Calls and puts share one table with an ENUM."""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Date, Enum, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import (
    AsciiKeyType,
    Base,
    PriceType,
    TsType,
    symbol_fk_column,
)


class OptionType(enum.StrEnum):
    CALL = "call"
    PUT = "put"


OPTION_TYPE_ENUM = Enum(
    OptionType,
    values_callable=lambda e: [m.value for m in e],
    name="option_type",
    native_enum=True,
)

# OCC-style contract symbols: root (up to 6) + YYMMDD + C/P + 8-digit
# strike. 'AAPL260918C00250000' is 19; 32 leaves room for a six-character
# root and then some, and stays well inside an index's key size.
CONTRACT_SYMBOL_LENGTH = 32


class OptionExpiration(Base):
    """One expiry Yahoo offered for a symbol on a given day.

    The expiry list comes free with the first chain request, so this table
    is complete while `option_quotes` covers only the fetched expiries."""

    __tablename__ = "option_expirations"
    __table_args__ = (Index("ix_option_expirations_as_of", "as_of_date"),)

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    expiry_date: Mapped[date] = mapped_column(Date, primary_key=True)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class OptionQuote(Base):
    """One contract, as it stood on `as_of_date`.

    Columns are exactly what `Ticker._options2df` puts on the frame;
    Greeks are absent because Yahoo does not send them."""

    __tablename__ = "option_quotes"
    __table_args__ = (
        Index("ix_option_quotes_as_of", "as_of_date"),
        Index("ix_option_quotes_expiry", "expiry_date"),
        # A contract with no strike is not a contract; the rest of the
        # row is nullable because a quiet series has no trade and no
        # quote, and that is data rather than damage.
        CheckConstraint('"strike" >= 0', name="ck_option_quotes_strike_nonneg"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    expiry_date: Mapped[date] = mapped_column(Date, primary_key=True)
    option_type: Mapped[OptionType] = mapped_column(OPTION_TYPE_ENUM, primary_key=True)
    contract_symbol: Mapped[str] = mapped_column(
        AsciiKeyType(CONTRACT_SYMBOL_LENGTH), primary_key=True
    )

    strike: Mapped[Decimal] = mapped_column(PriceType(), nullable=False)
    last_price: Mapped[Decimal | None] = mapped_column(PriceType())
    bid: Mapped[Decimal | None] = mapped_column(PriceType())
    ask: Mapped[Decimal | None] = mapped_column(PriceType())
    change: Mapped[Decimal | None] = mapped_column(PriceType())
    #: A plain number, as Yahoo sends it: 25 means 25 %.
    percent_change: Mapped[Decimal | None] = mapped_column(PriceType())
    #: Also a plain number, and NOT a percentage: 0.42 means 42 % vol.
    implied_volatility: Mapped[Decimal | None] = mapped_column(PriceType())
    volume: Mapped[int | None] = mapped_column(BigInteger)
    open_interest: Mapped[int | None] = mapped_column(BigInteger)
    in_the_money: Mapped[bool | None] = mapped_column(Boolean)
    #: Text rather than an enum: an unknown value must not drop the whole symbol.
    contract_size: Mapped[str | None] = mapped_column(String(16, collation="C"))
    currency: Mapped[str | None] = mapped_column(String(8, collation="C"))
    #: The contract's own last trade, which can be days old on a quiet
    #: series -- that staleness is the point of keeping the column.
    last_trade_ts_utc: Mapped[datetime | None] = mapped_column(TsType())
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
