"""Valuation measure datasets (`get_valuation_measures`).

`get_valuation_measures`'s frame has the SAME shape as the financial
statements: index is the item label, columns are periods. No new table or
normalize logic is needed; `StatementDataset` is reused with
`statement='valuation'`, the only difference being that column labels must
first be converted to dates.

Source (yfinance 1.7.0 `scrapers/quote.py:739-830`) pulls this from the same
fundamentals-timeseries endpoint as the statements; values are raw floats
(NOT strings like the old key-statistics scrape's '3.76T' format).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

import pandas as pd

from yfin import normalize as nz
from yfin.client import call_optional, call_yahoo
from yfin.datasets.base import NormalizedResult, SyncContext
from yfin.datasets.financials.statements import StatementDataset
from yfin.datasets.payloads import StatementPayload
from yfin.datasets.registry import register
from yfin.logging_setup import get_logger
from yfin.models.financials import API_FREQ, StatementFreq, StatementKind

log = get_logger(__name__)

# The source's one non-period column. Source builds column labels as
# f"{d.month}/{d.day}/{d.year}" (quote.py:815).
CURRENT_COLUMN = "Current"
COLUMN_FORMAT = "%m/%d/%Y"


def period_columns(frame: pd.DataFrame, *, symbol: str, dataset: str) -> pd.DataFrame:
    """Convert 'M/D/YYYY' columns to period-end timestamps; drop the rest.

    `Current` is DROPPED: it has no period-end date (the PK component would
    be null) and its value depends on the price at fetch time -- a column
    that goes stale in a permanent archive, same class as `adj_close`'s
    exclusion on the price-history side. Current market value already comes
    via `info.marketCap`.

    Conversion is NOT left to `pd.Timestamp`: for a label like '1/2/2026',
    month/day order would depend on pandas's guess. The source format is
    known, so it is given explicitly; an unparseable label is dropped with a
    WARNING (a safety valve against library upgrades).
    """
    renamed: dict[Any, pd.Timestamp] = {}
    for column in frame.columns:
        label = str(column)
        if label == CURRENT_COLUMN:
            continue
        try:
            period_end = datetime.strptime(label, COLUMN_FORMAT).date()
        except ValueError:
            log.warning(
                "valuation column is not a period",
                symbol=symbol,
                dataset=dataset,
                column=label,
            )
            continue
        renamed[column] = pd.Timestamp(period_end)
    if not renamed:
        return pd.DataFrame()
    return frame[list(renamed)].rename(columns=renamed)


def quote_currency(ctx: SyncContext) -> str | None:
    """info.currency -- the QUOTE currency; BEST-EFFORT.

    Using `statements.financial_currency` (info.financialCurrency) here
    would be WRONG. Measured (THYAO.IS): financialCurrency=USD,
    currency=TRY, and valuation 'Market Cap' = 4.14e11 -- same order of
    magnitude as `info.marketCap` (4.08e11, TRY), about 30x its USD
    equivalent. So valuation measures are in the exchange's QUOTE currency,
    NOT the reporting currency. Ratios (P/E, P/S, PEG) are already
    unitless; the currency matters only for the two monetary measures here
    (Market Cap, Enterprise Value).

    The `info` cache is SHARED with the statements: no second request within
    the same ctx.
    """
    try:
        info = ctx.cached(
            "info", lambda: call_yahoo(ctx.ticker.get_info, what=f"info:{ctx.symbol}")
        )
    except Exception as exc:  # noqa: BLE001 - secondary field, does not fail the cell
        log.warning("quote currency unavailable", symbol=ctx.symbol, error=str(exc))
        return None
    if not isinstance(info, dict):
        return None
    return nz.to_str(info.get("currency"), max_len=8)


class ValuationDataset(StatementDataset):
    """A StatementDataset with `statement='valuation'`.

    `produces`, `gate_table`, `gate_key_columns`, and `normalize`'s body come
    from the base class; the hash gate works unchanged.
    """

    def __init__(self, name: str, freq: StatementFreq) -> None:
        super().__init__(name, StatementKind.VALUATION, freq)

    def fetch(self, ctx: SyncContext) -> StatementPayload:
        api_freq = API_FREQ[self.freq]
        # `periods=None`: the default of 5 truncates the frame CLIENT-SIDE
        # (quote.py:640-644) -- full history already comes back on the same
        # request, so truncating would throw away free data. This cache key
        # is separate from the statements'; the same Ticker holds two
        # distinct endpoints.
        frame = ctx.cached(
            f"valuation:{api_freq}",
            lambda: call_optional(
                lambda: ctx.ticker.get_valuation_measures(freq=api_freq, periods=None),
                what=f"{self.name}:{ctx.symbol}",
            ),
        )
        return StatementPayload(
            frame=frame, currency=quote_currency(ctx), fetched_at=ctx.fetched_at
        )

    def normalize(self, raw: StatementPayload, symbol: str) -> NormalizedResult:
        frame = raw.frame
        if nz.is_empty_result(frame):
            return NormalizedResult()
        assert isinstance(frame, pd.DataFrame)
        dated = period_columns(frame, symbol=symbol, dataset=self.name)
        if dated.empty:
            # Only 'Current' came back: no period to write, and no error either.
            return NormalizedResult()
        return super().normalize(replace(raw, frame=dated), symbol)


# 'trailing' is NOT REGISTERED. Measured (AAPL, freq='trailing'): 13 columns
# with IRREGULAR dates (9/2, 9/1, 8/27, 8/11, 8/10, 7/31/2026, 10/3/2025),
# and within the same column some measures are NaN -- Market Cap populated
# on 9/2, Trailing P/E on 9/1. These are point-in-time observation
# timestamps, NOT period ends: every run would produce new `period_end`
# rows and make the EAV (symbol, table, freq, period) grain meaningless.
# 'monthly' is also NOT REGISTERED: adding a new StatementFreq member would
# extend a native ENUM SHARED by two tables; if ever needed, add MONTHLY
# and one line to this spec list.
_SPECS: tuple[tuple[str, StatementFreq], ...] = (
    ("valuation_measures", StatementFreq.ANNUAL),
    ("quarterly_valuation_measures", StatementFreq.QUARTERLY),
)

for _name, _freq in _SPECS:
    register(ValuationDataset(_name, _freq))
