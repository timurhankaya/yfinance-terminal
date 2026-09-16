"""insider_purchases dataset -> insider_activity.

Source presents one record as seven rows: column 0 holds the labels and its
header the period (`Insider Purchases Last 6m`). Read by position, pivoted.
"""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

from yfin.core import normalize as nz
from yfin.core.families import DataFamily
from yfin.core.logging_setup import get_logger
from yfin.datasets.asof_base import AsOfDataset, asof_produces
from yfin.datasets.base import NormalizedResult, SyncContext
from yfin.datasets.common import note_unmapped, to_big_value
from yfin.datasets.exposure import ApiExposure
from yfin.datasets.payloads import AsOfFramePayload
from yfin.datasets.registry import register
from yfin.ingest.client import call_optional
from yfin.storage.contracts import TableWrite

log = get_logger(__name__)

TABLE = "insider_activity"
PERIOD_PATTERN = re.compile(r"Insider Purchases Last (\S+)")
PERIOD_LABEL_LENGTH = 8
SHARES_COLUMN = "Shares"
TRANS_COLUMN = "Trans"

# Row label -> (Shares column, Trans column). An unmapped label is a signal
# to investigate, not data loss.
SHARE_ROWS: dict[str, tuple[str, str | None]] = {
    "Purchases": ("purchases_shares", "purchases_trans"),
    "Sales": ("sales_shares", "sales_trans"),
    "Net Shares Purchased (Sold)": ("net_shares", "net_trans"),
    "Total Insider Shares Held": ("total_insider_shares", None),
}
# Percentage rows arrive in the `Shares` column but are PriceType().
PCT_ROWS: dict[str, str] = {
    "% Net Shares Purchased (Sold)": "net_pct",
    "% Buy Shares": "buy_pct",
    "% Sell Shares": "sell_pct",
}

DATA_COLUMNS = (
    "period_label",
    *(column for column, _ in SHARE_ROWS.values()),
    *(column for _, column in SHARE_ROWS.values() if column is not None),
    *PCT_ROWS.values(),
)


class InsiderPurchasesDataset(AsOfDataset[AsOfFramePayload]):
    name = "insider_purchases"
    depends_on = ("symbols",)
    produces = asof_produces(TABLE)
    gate_source_tables = (TABLE,)
    api = (
        ApiExposure(
            family=DataFamily.HOLDERS,
            table="insider_activity",
            sort_key=("as_of_date",),
            descending=True,
            description="Aggregated insider buying and selling.",
        ),
    )

    def fetch(self, ctx: SyncContext) -> AsOfFramePayload:
        frame = call_optional(ctx.ticker.get_insider_purchases, what=f"{self.name}:{ctx.symbol}")
        return AsOfFramePayload(frame=frame, fetched_at=ctx.fetched_at)

    def normalize(self, raw: AsOfFramePayload, symbol: str) -> NormalizedResult:
        frame = raw.frame
        if nz.is_empty_result(frame):
            return NormalizedResult()
        assert isinstance(frame, pd.DataFrame)

        header = str(frame.columns[0])
        match = PERIOD_PATTERN.match(header)
        if match is None:
            # period_label is NOT NULL: no row can be written if the pattern fails.
            log.debug("unparsable insider period header", symbol=symbol, header=header)
            return NormalizedResult()
        period_label = match.group(1)[:PERIOD_LABEL_LENGTH]

        labels = frame.iloc[:, 0]
        shares = frame[SHARES_COLUMN] if SHARES_COLUMN in frame.columns else None
        trans = frame[TRANS_COLUMN] if TRANS_COLUMN in frame.columns else None

        row: dict[str, Any] = {
            "symbol": symbol,
            "as_of_date": raw.fetched_at.date(),
            "period_label": period_label,
        }
        unknown: list[str] = []
        for position, label in enumerate(labels):
            key = str(label)
            share_value = None if shares is None else shares.iloc[position]
            trans_value = None if trans is None else trans.iloc[position]
            if key in SHARE_ROWS:
                share_column, trans_column = SHARE_ROWS[key]
                row[share_column] = to_big_value(share_value)
                if trans_column is not None:
                    row[trans_column] = nz.to_int(trans_value)
            elif key in PCT_ROWS:
                row[PCT_ROWS[key]] = nz.to_decimal(share_value)
            else:
                unknown.append(key)

        if unknown:
            note_unmapped(self.name, sorted(unknown), symbol=symbol)

        row["fetched_at"] = raw.fetched_at
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


register(InsiderPurchasesDataset(), group="holders")
