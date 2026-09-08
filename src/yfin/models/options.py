"""Option expirations and chains.

Two tables because there are two facts. `option_expirations` records
WHICH expiries existed on a given day -- once an expiry drops off the
list, nothing else in the archive remembers it was ever offered.
`option_quotes` holds the chain itself, and it is only ever fetched for
the first few expiries (`yf_option_expiries`), so the list is not a
summary of the quotes: twenty rows against four expiries' worth of
contracts.

One table for calls and puts, separated by an ENUM. Their column sets are
identical by construction -- yfinance builds both with the same
`reindex` -- and this codebase's rule for that case is one table plus a
discriminator (`institutional_holders` + `holder_type` set the
precedent).
"""

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

    Written from the SAME request that returns the first chain: the
    expiry list is free, and every chain after the first costs one more
    request. That asymmetry is why this table can be complete while
    `option_quotes` is deliberately not.
    """

    __tablename__ = "option_expirations"
    __table_args__ = (Index("ix_option_expirations_as_of", "as_of_date"),)

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    expiry_date: Mapped[date] = mapped_column(Date, primary_key=True)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class OptionQuote(Base):
    """One contract, as it stood on `as_of_date`.

    Every column here is one of the fourteen `Ticker._options2df` forces
    onto the frame; nothing is derived. Greeks are absent because Yahoo
    does not send them, not because they were dropped.
    """

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
    #: 'REGULAR' in everything measured; kept as text rather than an enum
    #: because a second value would otherwise drop the whole symbol.
    contract_size: Mapped[str | None] = mapped_column(String(16, collation="C"))
    currency: Mapped[str | None] = mapped_column(String(8, collation="C"))
    #: The contract's own last trade, which can be days old on a quiet
    #: series -- that staleness is the point of keeping the column.
    last_trade_ts_utc: Mapped[datetime | None] = mapped_column(TsType())
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
