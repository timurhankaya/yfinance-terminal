"""earnings_history dataset.

Not as-of: the source returns a quarter end. `quarter_end` is taken from a
tz-naive Timestamp via `.date()` with no timezone conversion -- a fiscal
quarter is a calendar label, not an instant.

The source returns exactly four quarters per symbol (measured (4, 4) on
17 symbols); `--start` does not extend that history, it only filters rows.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from yfin.core import normalize as nz
from yfin.core.logging_setup import get_logger
from yfin.datasets.base import Dataset, NormalizedResult, SyncContext, TableWrite
from yfin.datasets.common import in_range
from yfin.datasets.payloads import RangedFramePayload
from yfin.datasets.registry import register
from yfin.ingest.client import call_optional

log = get_logger(__name__)

TABLE = "earnings_history"
COLUMNS = (
    ("epsActual", "eps_actual"),
    ("epsEstimate", "eps_estimate"),
    ("epsDifference", "eps_difference"),
    ("surprisePercent", "surprise_percent"),
)


class EarningsHistoryDataset(Dataset[RangedFramePayload]):
    name = "earnings_history"
    depends_on = ("symbols",)
    produces = (TABLE,)
    date_range = "filter"

    def fetch(self, ctx: SyncContext) -> RangedFramePayload:
        frame = call_optional(
            ctx.ticker.get_earnings_history, what=f"{self.name}:{ctx.symbol}"
        )
        return RangedFramePayload(
            frame=frame, fetched_at=ctx.fetched_at, start=ctx.start, end=ctx.end
        )

    def normalize(self, raw: RangedFramePayload, symbol: str) -> NormalizedResult:
        frame = raw.frame
        if nz.is_empty_result(frame):
            return NormalizedResult()
        assert isinstance(frame, pd.DataFrame)

        ranged = raw.start is not None or raw.end is not None
        rows: dict[Any, dict[str, Any]] = {}

        for index, record in frame.iterrows():
            quarter_end = nz.to_local_date(index)
            if quarter_end is None:
                log.warning("earnings history row has no quarter", symbol=symbol)
                continue
            if ranged and not in_range(quarter_end, raw.start, raw.end):
                continue

            row: dict[str, Any] = {
                "symbol": symbol,
                "quarter_end": quarter_end,
                **{
                    column: nz.to_decimal(record.get(source)) for source, column in COLUMNS
                },
                "fetched_at": raw.fetched_at,
            }
            rows[quarter_end] = row

        if not rows:
            return NormalizedResult()

        return NormalizedResult(
            writes=[
                TableWrite(
                    table=TABLE,
                    rows=list(rows.values()),
                    key_columns=("symbol", "quarter_end"),
                    update_columns=(*(c for _, c in COLUMNS), "fetched_at"),
                )
            ]
        )


register(EarningsHistoryDataset())
