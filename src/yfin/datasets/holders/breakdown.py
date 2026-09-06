"""major_holders dataset -> holder_breakdown.

Source turns the `majorHoldersBreakdown` dict into a single-column ('Value')
frame (holders.py:139-146); the index holds KEY names. The same four keys
were measured across 19/19 symbols, so typed columns are used instead of EAV.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from yfin import normalize as nz
from yfin.client import call_optional
from yfin.datasets.asof_base import AsOfDataset, asof_produces
from yfin.datasets.base import NormalizedResult, SyncContext, TableWrite
from yfin.datasets.payloads import AsOfFramePayload
from yfin.datasets.registry import register
from yfin.logging_setup import get_logger

log = get_logger(__name__)

TABLE = "holder_breakdown"
VALUE_COLUMN = "Value"
FIELDS: tuple[tuple[str, str], ...] = (
    ("insidersPercentHeld", "insiders_pct_held"),
    ("institutionsPercentHeld", "institutions_pct_held"),
    ("institutionsFloatPercentHeld", "institutions_float_pct_held"),
)
COUNT_SOURCE = "institutionsCount"
DATA_COLUMNS = (*(column for _, column in FIELDS), "institutions_count")


class MajorHoldersDataset(AsOfDataset[AsOfFramePayload]):
    name = "major_holders"
    depends_on = ("symbols",)
    produces = asof_produces(TABLE)

    def fetch(self, ctx: SyncContext) -> AsOfFramePayload:
        frame = call_optional(ctx.ticker.get_major_holders, what=f"{self.name}:{ctx.symbol}")
        return AsOfFramePayload(frame=frame, fetched_at=ctx.fetched_at)

    def normalize(self, raw: AsOfFramePayload, symbol: str) -> NormalizedResult:
        frame = raw.frame
        if nz.is_empty_result(frame):
            return NormalizedResult()
        assert isinstance(frame, pd.DataFrame)
        if VALUE_COLUMN not in frame.columns:
            log.warning("major holders frame has no Value column", symbol=symbol)
            return NormalizedResult()

        values = {str(index): value for index, value in frame[VALUE_COLUMN].items()}
        unmapped = sorted(set(values) - {src for src, _ in FIELDS} - {COUNT_SOURCE})
        if unmapped:
            log.warning("unmapped keys", dataset=self.name, symbol=symbol, keys=unmapped)

        row: dict[str, Any] = {
            "symbol": symbol,
            "as_of_date": raw.fetched_at.date(),
            **{column: nz.to_decimal(values.get(source)) for source, column in FIELDS},
            # Source sends this as a float (7750.0).
            "institutions_count": nz.to_int(values.get(COUNT_SOURCE)),
            "fetched_at": raw.fetched_at,
        }
        return NormalizedResult(
            writes=[
                TableWrite(
                    table=TABLE,
                    rows=[row],
                    key_columns=("symbol", "as_of_date"),
                    update_columns=(*DATA_COLUMNS, "fetched_at"),
                )
            ]
        )


register(MajorHoldersDataset())
