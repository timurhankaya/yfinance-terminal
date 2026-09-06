"""Response models for the core market endpoints.

Every monetary or large-integer field is a string. The database stores
prices as Numeric(28,12) and counts as Numeric(38,0) exactly so they are
not floats; emitting them as JSON numbers would silently undo that at the
API boundary, where the loss is least visible and hardest to reverse.

The bar model covers three tables with different columns, so the fields
only some of them have are optional and documented as such. The
alternative -- three near-identical schemas -- would push the difference
onto every client instead of describing it once.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


class SymbolSummary(BaseModel):
    symbol: str
    isin: str | None = None
    quote_type: str | None = None
    exchange: str | None = None
    full_exchange_name: str | None = None
    currency: str | None = None
    timezone: str | None = None
    short_name: str | None = None
    long_name: str | None = None
    first_trade_date: datetime | None = None
    is_active: bool


class SymbolDetail(SymbolSummary):
    info: dict[str, object] | None = Field(
        default=None,
        description=(
            "The latest identity snapshot from the source, or null if the symbol "
            "has never been synced."
        ),
    )


class Bar(BaseModel):
    symbol: str
    ts_utc: datetime = Field(description="Bar open, UTC.")

    bar_interval: str | None = Field(
        default=None,
        description="Present for intraday and weekly/monthly bars; absent for 1d.",
    )
    session_date: date | None = Field(
        default=None,
        description=(
            "1d only: the exchange's SESSION day. Not interchangeable with "
            "local_date -- a bar late in the local day can belong to the next "
            "session."
        ),
    )
    local_date: date | None = Field(
        default=None,
        description=(
            "Intraday and periodic bars: the bar's local CALENDAR day, which is "
            "not the session day."
        ),
    )

    open: str | None = None
    high: str | None = None
    low: str | None = None
    close: str | None = None
    adj_close: str | None = Field(
        default=None, description="1d only: dividend-adjusted close."
    )
    volume: int | None = None
    is_extended: bool | None = Field(
        default=None,
        description=(
            "Intraday only. Weekly and monthly bars have no such flag: outside "
            "regular hours has no meaning above daily."
        ),
    )


class Action(BaseModel):
    symbol: str
    action_date: date
    action_type: str = Field(description="DIVIDEND, SPLIT or CAPITAL_GAIN.")
    action_value: str


class FinancialFactOut(BaseModel):
    period_end: date
    item_key: str
    value: str
    currency: str | None = Field(
        default=None,
        description=(
            "Reporting currency of the statement, which can differ from the "
            "symbol's trading currency."
        ),
    )
