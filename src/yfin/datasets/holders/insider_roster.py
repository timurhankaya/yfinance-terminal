"""insider_roster_holders dataset -> insider_roster.

Source column set is 7 / 9 / 11 depending on symbol, and the ORDER IS NOT
FIXED either (holders.py:186-200 conditionally renames fields). Every field
is therefore read with `record.get(...)`; assuming position or a fixed
order would silently lose data.

`positionSummary` / `positionSummaryDate` were seen only for NVDA, but there
they were the ONLY share info for a person; without a column for them, that
row's entire share data would be NULL.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from yfin import normalize as nz
from yfin.client import call_optional
from yfin.datasets.asof_base import AsOfDataset, asof_produces
from yfin.datasets.base import NormalizedResult, SyncContext, TableWrite
from yfin.datasets.common import blank_to_none, key_value, to_big_value, to_datetime_value
from yfin.datasets.payloads import AsOfFramePayload
from yfin.datasets.registry import register
from yfin.logging_setup import get_logger

log = get_logger(__name__)

TABLE = "insider_roster"
NAME_LENGTH = 255
KEY_COLUMNS = ("symbol", "as_of_date", "name")
SCOPE_COLUMNS = ("symbol", "as_of_date")
DATA_COLUMNS = (
    "position",
    "url",
    "most_recent_transaction",
    "latest_transaction_date",
    "position_direct_date",
    "position_indirect_date",
    "shares_owned_directly",
    "shares_owned_indirectly",
    "position_summary",
    "position_summary_date",
)
MAPPED_SOURCES = frozenset(
    {
        "Name",
        "Position",
        "URL",
        "Most Recent Transaction",
        "Latest Transaction Date",
        "Position Direct Date",
        "Position Indirect Date",
        "Shares Owned Directly",
        "Shares Owned Indirectly",
        "positionSummary",
        "positionSummaryDate",
    }
)


class InsiderRosterDataset(AsOfDataset[AsOfFramePayload]):
    name = "insider_roster_holders"
    depends_on = ("symbols",)
    produces = asof_produces(TABLE)

    def fetch(self, ctx: SyncContext) -> AsOfFramePayload:
        frame = call_optional(
            ctx.ticker.get_insider_roster_holders, what=f"{self.name}:{ctx.symbol}"
        )
        return AsOfFramePayload(frame=frame, fetched_at=ctx.fetched_at)

    def normalize(self, raw: AsOfFramePayload, symbol: str) -> NormalizedResult:
        frame = raw.frame
        if nz.is_empty_result(frame):
            return NormalizedResult()
        assert isinstance(frame, pd.DataFrame)

        unmapped = sorted(str(c) for c in frame.columns if str(c) not in MAPPED_SOURCES)
        if unmapped:
            log.warning("unmapped keys", dataset=self.name, symbol=symbol, keys=unmapped)

        as_of = raw.fetched_at.date()
        rows: dict[str, dict[str, Any]] = {}

        for _, record in frame.iterrows():
            person = key_value(
                record.get("Name"),
                NAME_LENGTH,
                field="name",
                dataset=self.name,
                symbol=symbol,
            )
            if person is None:
                continue
            rows[person] = {
                "symbol": symbol,
                "as_of_date": as_of,
                "name": person,
                "position": blank_to_none(record.get("Position"), max_len=64),
                "url": blank_to_none(record.get("URL")),
                "most_recent_transaction": blank_to_none(
                    record.get("Most Recent Transaction"), max_len=64
                ),
                "latest_transaction_date": to_datetime_value(
                    record.get("Latest Transaction Date")
                ),
                "position_direct_date": to_datetime_value(record.get("Position Direct Date")),
                "position_indirect_date": to_datetime_value(
                    record.get("Position Indirect Date")
                ),
                "shares_owned_directly": to_big_value(record.get("Shares Owned Directly")),
                "shares_owned_indirectly": to_big_value(record.get("Shares Owned Indirectly")),
                "position_summary": to_big_value(record.get("positionSummary")),
                "position_summary_date": to_datetime_value(record.get("positionSummaryDate")),
                "fetched_at": raw.fetched_at,
            }

        return NormalizedResult(
            writes=[
                TableWrite(
                    table=TABLE,
                    rows=list(rows.values()),
                    key_columns=KEY_COLUMNS,
                    update_columns=(*DATA_COLUMNS, "fetched_at"),
                    mode="replace_scope",
                    scope_columns=SCOPE_COLUMNS,
                    # Scope is given explicitly so that when the roster
                    # shrinks, stale people don't linger under the SAME as-of
                    # date.
                    scope_values=({"symbol": symbol, "as_of_date": as_of},),
                )
            ]
        )


register(InsiderRosterDataset())
