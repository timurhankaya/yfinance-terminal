"""upgrades_downgrades dataset -> analyst_grade_changes.

Not as-of: the source carries each row's own date (`epochGradeDate`). A
pure upsert -- with `replace_scope`, old records that fall off the
source's ~1000-row cap would be deleted on every run.

The official docs list four columns; measurement found seven:
`priceTargetAction`, `currentPriceTarget`, `priorPriceTarget` also appear
(consistent across 15 symbols).
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from yfin.core import normalize as nz
from yfin.core.families import DataFamily
from yfin.core.logging_setup import get_logger
from yfin.datasets.base import Dataset, NormalizedResult, SyncContext
from yfin.datasets.common import blank_to_none, in_range, key_value
from yfin.datasets.exposure import ApiExposure
from yfin.datasets.payloads import RangedFramePayload
from yfin.datasets.registry import register
from yfin.ingest.client import call_optional
from yfin.storage.contracts import TableWrite

log = get_logger(__name__)

TABLE = "analyst_grade_changes"
FIRM_LENGTH = 64
KEY_COLUMNS = ("symbol", "grade_ts_utc", "firm")
UPDATE_COLUMNS = (
    "to_grade",
    "from_grade",
    "action",
    "price_target_action",
    "current_price_target",
    "prior_price_target",
    "fetched_at",
)
# Source columns mapped into the table; any other key logs a warning so a
# new source column does not silently vanish.
MAPPED_SOURCES = frozenset(
    {
        "Firm",
        "ToGrade",
        "FromGrade",
        "Action",
        "priceTargetAction",
        "currentPriceTarget",
        "priorPriceTarget",
    }
)


class UpgradesDowngradesDataset(Dataset[RangedFramePayload]):
    name = "upgrades_downgrades"
    depends_on = ("symbols",)
    produces = (TABLE,)
    # Source returns a fixed window; the date range only filters rows.
    date_range = "filter"
    api = ApiExposure(
        family=DataFamily.FUNDAMENTALS,
        table="analyst_grade_changes",
        sort_key=("grade_ts_utc", "firm"),
        descending=True,
        description="Analyst upgrades and downgrades, newest first.",
    )

    def fetch(self, ctx: SyncContext) -> RangedFramePayload:
        frame = call_optional(
            ctx.ticker.get_upgrades_downgrades, what=f"{self.name}:{ctx.symbol}"
        )
        return RangedFramePayload(
            frame=frame, fetched_at=ctx.fetched_at, start=ctx.start, end=ctx.end
        )

    def normalize(self, raw: RangedFramePayload, symbol: str) -> NormalizedResult:
        frame = raw.frame
        if nz.is_empty_result(frame):
            return NormalizedResult()
        assert isinstance(frame, pd.DataFrame)

        unmapped = sorted(str(c) for c in frame.columns if str(c) not in MAPPED_SOURCES)
        if unmapped:
            log.warning("unmapped keys", dataset=self.name, symbol=symbol, keys=unmapped)

        ranged = raw.start is not None or raw.end is not None
        rows: dict[tuple[Any, ...], dict[str, Any]] = {}

        for index, record in frame.iterrows():
            # Index is tz-naive but built from epochGradeDate (a UTC epoch
            # second), so it is already UTC -- no second tz conversion.
            ts_utc = nz.to_datetime_utc(index)
            if ts_utc is None:
                log.warning("grade change has no timestamp", symbol=symbol)
                continue
            if ranged and not in_range(ts_utc.date(), raw.start, raw.end):
                continue

            # firm is part of the PK: a blank string would collapse two
            # distinct records into one row, and so would a truncated name.
            firm = key_value(
                record.get("Firm"),
                FIRM_LENGTH,
                field="firm",
                dataset=self.name,
                symbol=symbol,
            )
            if firm is None:
                continue

            row = {
                "symbol": symbol,
                "grade_ts_utc": ts_utc,
                "firm": firm,
                "to_grade": blank_to_none(record.get("ToGrade"), max_len=32),
                "from_grade": blank_to_none(record.get("FromGrade"), max_len=32),
                "action": blank_to_none(record.get("Action"), max_len=16),
                "price_target_action": blank_to_none(
                    record.get("priceTargetAction"), max_len=16
                ),
                # 0.0 is a real value; it is not converted to NULL.
                "current_price_target": nz.to_decimal(record.get("currentPriceTarget")),
                "prior_price_target": nz.to_decimal(record.get("priorPriceTarget")),
                "fetched_at": raw.fetched_at,
            }
            rows[(ts_utc, firm)] = row

        if not rows:
            return NormalizedResult()

        return NormalizedResult(
            writes=[
                TableWrite(
                    table=TABLE,
                    rows=list(rows.values()),
                    key_columns=KEY_COLUMNS,
                    update_columns=UPDATE_COLUMNS,
                )
            ]
        )


register(UpgradesDowngradesDataset())
