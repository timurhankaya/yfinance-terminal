"""dividends, splits, capital_gains datasets.

All three feed from the repaired shared history frame: yfinance does not
forward `repair` to `get_dividends` etc., and each would cost a history() call.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from yfin.core import normalize as nz
from yfin.datasets.base import Dataset, NormalizedResult, SyncContext
from yfin.datasets.history import fetch_history_frame
from yfin.datasets.payloads import FramePayload
from yfin.datasets.registry import register
from yfin.storage.contracts import TableWrite


class _SeriesDataset(Dataset[FramePayload]):
    # All three feed from the shared history frame; the range passes to
    # that call, it does not filter rows.
    date_range = "api"
    # depends_on is not ("history",): that would make `--datasets
    # dividends` also write to price_history. The frame is called directly;
    # if history is also selected, ctx.cached shares the same call.
    depends_on = ("symbols",)
    table: str
    date_column: str
    value_column: str
    frame_column: str

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Declares this subclass as a consumer of the shared history frame.

        Derived from `table`/`date_column`: a wrong `shared_frame_watermark`
        reads None forever and refetches full history every run.
        """
        super().__init_subclass__(**kwargs)
        cls.shared_frame_watermark = (cls.table, cls.date_column)

    def fetch(self, ctx: SyncContext) -> FramePayload:
        return fetch_history_frame(ctx)

    def normalize(self, raw: FramePayload, symbol: str) -> NormalizedResult:
        # Can also be None; '.empty' alone is not enough
        if nz.is_empty_result(raw):
            return NormalizedResult()

        frame: pd.DataFrame = raw
        if self.frame_column not in frame.columns:
            # Column set varies by symbol ('Capital Gains' is absent for a
            # non-fund symbol). An empty result is 'empty', not 'failed'.
            return NormalizedResult()
        series = frame[self.frame_column]
        # Dedupe by key: if the source has two records land on the same
        # local date (two timestamps at different times), attempted=2 /
        # verified=1 would mark the cell 'failed' incorrectly. Last record
        # wins -- same pattern as shares_full.
        by_date: dict[Any, dict[str, Any]] = {}
        for index, value in series.items():
            # The frame carries 0 on event-free days; only real events
            # enter the table.
            if value is None or float(value) == 0.0:
                continue
            # ex_date is also a local date; the dividends/splits index comes
            # at a different time of day than history's.
            when = nz.to_local_date(index)
            amount = nz.to_decimal(value)
            if when is None or amount is None:
                continue
            by_date[when] = {
                "symbol": symbol,
                self.date_column: when,
                self.value_column: amount,
            }

        rows: list[dict[str, Any]] = list(by_date.values())
        if not rows:
            return NormalizedResult()

        return NormalizedResult(
            writes=[
                TableWrite(
                    table=self.table,
                    rows=rows,
                    key_columns=("symbol", self.date_column),
                    update_columns=(self.value_column,),
                )
            ]
        )


class DividendsDataset(_SeriesDataset):
    name = "dividends"
    produces = ("dividends",)
    table = "dividends"
    date_column = "ex_date"
    value_column = "amount"
    frame_column = "Dividends"


class SplitsDataset(_SeriesDataset):
    name = "splits"
    produces = ("splits",)
    table = "splits"
    date_column = "split_date"
    value_column = "ratio"
    frame_column = "Stock Splits"


class CapitalGainsDataset(_SeriesDataset):
    """The 'Capital Gains' column only appears for funds; otherwise the result is empty."""

    name = "capital_gains"
    produces = ("capital_gains",)
    table = "capital_gains"
    date_column = "gain_date"
    value_column = "amount"
    frame_column = "Capital Gains"


register(DividendsDataset())
register(SplitsDataset())
register(CapitalGainsDataset())
