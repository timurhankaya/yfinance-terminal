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
from yfin.core.families import DataFamily
from yfin.core.logging_setup import get_logger
from yfin.datasets.base import Dataset, NormalizedResult, SyncContext
from yfin.datasets.common import in_range
from yfin.datasets.exposure import ApiExposure
from yfin.datasets.payloads import RangedFramePayload
from yfin.datasets.registry import register
from yfin.ingest.client import call_optional
from yfin.storage.contracts import TableWrite

log = get_logger(__name__)

TABLE = "earnings_history"
COLUMNS = (
    ("epsActual", "eps_actual"),
    ("epsEstimate", "eps_estimate"),
    ("epsDifference", "eps_difference"),
    ("surprisePercent", "surprise_percent"),
)
MAPPED_SOURCES = frozenset(source for source, _ in COLUMNS)


class EarningsHistoryDataset(Dataset[RangedFramePayload]):
    name = "earnings_history"
    depends_on = ("symbols",)
    produces = (TABLE,)
    date_range = "filter"
    api = (
        ApiExposure(
            family=DataFamily.FUNDAMENTALS,
            table="earnings_history",
            sort_key=("quarter_end",),
            descending=True,
            description="Reported versus estimated EPS, by quarter.",
        ),
    )

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

        # This table has no `raw_json`, so a column Yahoo adds is lost for
        # good rather than kept and promoted later. Every sibling in this
        # package says so when it happens; this one was the exception, and
        # a four-column allowlist has already been caught short once --
        # `grade_changes` documents finding seven where the docs said four.
        unmapped = sorted(
            str(column) for column in frame.columns if str(column) not in MAPPED_SOURCES
        )
        if unmapped:
            log.warning("unmapped keys", dataset=self.name, symbol=symbol, keys=unmapped)

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


register(EarningsHistoryDataset(), family="analysis")
