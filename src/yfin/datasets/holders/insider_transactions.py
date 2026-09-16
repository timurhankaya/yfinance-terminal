"""insider_transactions dataset.

Not as-of: plain upsert. The source can return fully identical rows; without
dedup `rows_verified != rows_attempted` marks the cell `failed`. Caps at 150 rows.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from yfin.core import normalize as nz
from yfin.core.families import DataFamily
from yfin.core.logging_setup import get_logger
from yfin.datasets.base import Dataset, NormalizedResult, SyncContext
from yfin.datasets.common import blank_to_none, in_range, key_value, note_unmapped, to_big_value
from yfin.datasets.exposure import ApiExposure
from yfin.datasets.payloads import RangedFramePayload
from yfin.datasets.registry import register
from yfin.ingest.client import call_optional
from yfin.storage.contracts import TableWrite

log = get_logger(__name__)

TABLE = "insider_transactions"
KEY_COLUMNS = ("symbol", "start_date", "fact_hash")
DATA_COLUMNS = (
    "insider",
    "position",
    "text",
    "transaction_label",
    "url",
    "shares",
    "value",
    "ownership",
)
MAPPED_SOURCES = frozenset(
    {
        "Start Date",
        "Insider",
        "Position",
        "URL",
        "Transaction",
        "Text",
        "Shares",
        "Value",
        "Ownership",
    }
)
# Fields that feed fact_hash; `start_date` is a separate PK component and
# `fetched_at` changes on every run.
HASH_FIELDS = ("insider", "position", "text", "shares", "value", "ownership")


class InsiderTransactionsDataset(Dataset[RangedFramePayload]):
    name = "insider_transactions"
    depends_on = ("symbols",)
    produces = (TABLE,)
    date_range = "filter"
    api = (
        ApiExposure(
            family=DataFamily.HOLDERS,
            table="insider_transactions",
            sort_key=("start_date", "fact_hash"),
            descending=True,
            description="Individual insider transactions.",
        ),
    )

    def fetch(self, ctx: SyncContext) -> RangedFramePayload:
        frame = call_optional(
            ctx.ticker.get_insider_transactions, what=f"{self.name}:{ctx.symbol}"
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
            note_unmapped(self.name, unmapped, symbol=symbol)

        deduped = frame.drop_duplicates()
        dropped = len(frame) - len(deduped)
        if dropped:
            log.debug("dropped duplicate insider rows", symbol=symbol, rows=dropped)

        ranged = raw.start is not None or raw.end is not None
        rows: dict[tuple[Any, ...], dict[str, Any]] = {}

        for _, record in deduped.iterrows():
            start_date = nz.to_local_date(record.get("Start Date"))
            if start_date is None:
                log.debug("insider transaction has no start date", symbol=symbol)
                continue
            if ranged and not in_range(start_date, raw.start, raw.end):
                continue

            values: dict[str, Any] = {
                "insider": nz.to_str(record.get("Insider"), max_len=255),
                # '' -> NULL: the source sends an empty Position.
                "position": blank_to_none(record.get("Position"), max_len=64),
                "text": blank_to_none(record.get("Text"), max_len=255),
                "transaction_label": blank_to_none(record.get("Transaction"), max_len=64),
                "url": blank_to_none(record.get("URL")),
                "shares": to_big_value(record.get("Shares")),
                # Can be NaN in every row of a symbol.
                "value": to_big_value(record.get("Value")),
                # 'D', 'I', and 'D/I'. Feeds `fact_hash`, i.e. a PK
                # component -> NOT TRUNCATED: truncation could merge two
                # DIFFERENT ownership types into the same hash.
                "ownership": key_value(
                    record.get("Ownership"),
                    8,
                    field="ownership",
                    dataset=self.name,
                    symbol=symbol,
                )
                if record.get("Ownership") not in (None, "")
                else None,
            }
            # Hashed from the raw string: 'Sale' and 'sale' are distinct
            # rows regardless of the DB collation. Do not normalize case.
            digest = nz.content_hash(
                {
                    name: (str(values[name]) if values[name] is not None else None)
                    for name in HASH_FIELDS
                }
            )
            row = {
                "symbol": symbol,
                "start_date": start_date,
                "fact_hash": digest[:16],
                **values,
                "fetched_at": raw.fetched_at,
            }
            rows[(start_date, row["fact_hash"])] = row

        if not rows:
            return NormalizedResult()

        return NormalizedResult(
            writes=[
                TableWrite(
                    table=TABLE,
                    rows=list(rows.values()),
                    key_columns=KEY_COLUMNS,
                    update_columns=(*DATA_COLUMNS, "fetched_at"),
                )
            ]
        )


register(InsiderTransactionsDataset(), group="holders")
