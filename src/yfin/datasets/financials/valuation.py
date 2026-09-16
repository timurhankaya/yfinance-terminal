"""Valuation measure datasets (`get_valuation_measures`).

Same frame shape as the statements, so `StatementDataset` is reused with
`statement='valuation'`; only the column labels must first become dates.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

import pandas as pd

from yfin.core import normalize as nz
from yfin.core.logging_setup import get_logger
from yfin.datasets.base import NormalizedResult, SyncContext
from yfin.datasets.financials.statements import StatementDataset
from yfin.datasets.payloads import StatementPayload
from yfin.datasets.registry import register
from yfin.ingest.client import call_optional, call_yahoo
from yfin.models.financials import API_FREQ, StatementFreq, StatementKind

log = get_logger(__name__)

# The source's one non-period column. Source builds column labels as
# f"{d.month}/{d.day}/{d.year}" (quote.py:815).
CURRENT_COLUMN = "Current"
COLUMN_FORMAT = "%m/%d/%Y"


def period_columns(frame: pd.DataFrame, *, symbol: str, dataset: str) -> pd.DataFrame:
    """Convert 'M/D/YYYY' columns to period-end timestamps; drop the rest.

    `Current` is dropped: no period end, and its value goes stale. The
    format is given explicitly so month/day order is never pandas's guess.
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

    Not `financial_currency`: valuation measures are in the exchange's
    quote currency, not the reporting currency.
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


# 'trailing' is not registered: its columns are observation timestamps,
# not period ends, and would break the (symbol, table, freq, period) grain.
# 'monthly' would extend a native ENUM shared by two tables.
_SPECS: tuple[tuple[str, StatementFreq], ...] = (
    ("valuation_measures", StatementFreq.ANNUAL),
    ("quarterly_valuation_measures", StatementFreq.QUARTERLY),
)

for _name, _freq in _SPECS:
    register(ValuationDataset(_name, _freq))
