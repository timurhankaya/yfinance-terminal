"""shares_full dataset."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from yfin import normalize as nz
from yfin.client import call_yahoo
from yfin.config import get_settings
from yfin.datasets.base import Dataset, NormalizedResult, SyncContext, TableWrite
from yfin.datasets.common import EPOCH_START, date_range_kwargs
from yfin.datasets.payloads import SeriesPayload
from yfin.datasets.registry import register


class SharesFullDataset(Dataset[SeriesPayload]):
    name = "shares_full"
    depends_on = ("symbols",)
    produces = ("shares_full",)
    date_range = "api"

    def fetch(self, ctx: SyncContext) -> SeriesPayload:
        # --start/--end OVERRIDES the watermark.
        if ctx.start is not None or ctx.end is not None:
            kwargs = date_range_kwargs(ctx.start, ctx.end)
        else:
            watermark = ctx.watermark("shares_full", "as_of_date")
            if watermark is None:
                # start=None becomes 'end - 548 days' inside yfinance; for
                # AAPL, 353 of 420 rows are SILENTLY dropped (base.py:511).
                kwargs = {"start": EPOCH_START.isoformat()}
            else:
                overlap = get_settings().yf_incremental_overlap_days
                base = watermark.date() if isinstance(watermark, datetime) else watermark
                kwargs = {"start": (base - timedelta(days=overlap)).isoformat()}
        result: SeriesPayload = call_yahoo(
            lambda: ctx.ticker.get_shares_full(**kwargs),
            what=f"shares_full:{ctx.symbol}",
        )
        return result

    def normalize(self, raw: SeriesPayload, symbol: str) -> NormalizedResult:
        # SPY, BTC-USD, EURUSD=X, GC=F, ^GSPC -> returns None; calling
        # 'raw.empty' would raise AttributeError.
        if nz.is_empty_result(raw):
            return NormalizedResult()

        series: pd.Series[Any] = raw
        rows: list[dict[str, Any]] = []
        seen: set[tuple[date, int]] = set()
        for index, value in series.items():
            as_of = nz.to_local_date(index)
            shares = nz.to_int(value)
            if as_of is None or shares is None or shares < 0:
                continue
            # PK (symbol, as_of_date, shares): the source has different
            # values on the same date. A repeated date+value pair collapses
            # to one row.
            key = (as_of, shares)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "symbol": symbol,
                    "as_of_date": as_of,
                    "shares": shares,
                    "ts_utc": nz.to_datetime_utc(index),
                }
            )

        if not rows:
            return NormalizedResult()

        return NormalizedResult(
            writes=[
                TableWrite(
                    table="shares_full",
                    rows=rows,
                    key_columns=("symbol", "as_of_date", "shares"),
                    update_columns=("ts_utc",),
                )
            ]
        )


register(SharesFullDataset())
