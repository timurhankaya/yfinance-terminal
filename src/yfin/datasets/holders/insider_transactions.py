"""insider_transactions dataset.

NOT AS-OF: the source gives a transaction date. Plain upsert.

EXACT DEDUPLICATION IS MANDATORY. `fact_hash` alone is not enough: PFE
measured two rows IDENTICAL ON ALL NINE COLUMNS (BOSHOFF CHRISTOFFEL, 8741
shares, value 263716, 2025-02-21), hashes included. Without dedup, 34 rows
read would write 33, and `rows_verified != rows_attempted` would wrongly
produce `failed` on every run -- same rule as `earnings_dates`.

The source limit is 150 ROWS, NOT a time window: 12 of 24 symbols measured
returned exactly 150 rows, with the window shrinking to 12.4 months for WMT.
`--start 2024-01-01` does not extend this data further back.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from yfin.core import normalize as nz
from yfin.core.logging_setup import get_logger
from yfin.datasets.base import Dataset, NormalizedResult, SyncContext
from yfin.datasets.common import blank_to_none, in_range, key_value, to_big_value
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
            log.warning("unmapped keys", dataset=self.name, symbol=symbol, keys=unmapped)

        deduped = frame.drop_duplicates()
        dropped = len(frame) - len(deduped)
        if dropped:
            log.warning("dropped duplicate insider rows", symbol=symbol, rows=dropped)

        ranged = raw.start is not None or raw.end is not None
        rows: dict[tuple[Any, ...], dict[str, Any]] = {}

        for _, record in deduped.iterrows():
            start_date = nz.to_local_date(record.get("Start Date"))
            if start_date is None:
                log.warning("insider transaction has no start date", symbol=symbol)
                continue
            if ranged and not in_range(start_date, raw.start, raw.end):
                continue

            values: dict[str, Any] = {
                "insider": nz.to_str(record.get("Insider"), max_len=255),
                # '' -> NULL: measured an empty Position for BP.L.
                "position": blank_to_none(record.get("Position"), max_len=64),
                "text": blank_to_none(record.get("Text"), max_len=255),
                "transaction_label": blank_to_none(record.get("Transaction"), max_len=64),
                "url": blank_to_none(record.get("URL")),
                "shares": to_big_value(record.get("Shares")),
                # NaN in ALL rows for DIS and BP.L.
                "value": to_big_value(record.get("Value")),
                # 'D', 'I', and 'D/I' (XOM). Feeds `fact_hash`, i.e. a PK
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
            # Hash is computed in PYTHON from the raw string, so 'Sale' and
            # 'sale' are TWO SEPARATE rows. This is DELIBERATE and
            # unaffected by engine changes: the PK component is `fact_hash`,
            # and the comparison already happens here, in Python.
            #
            # MySQL's collation (utf8mb4_0900_ai_ci) would have seen the two
            # as equal, but the hash already distinguished them; PostgreSQL's
            # column is COLLATE "C" so the schema reaches the same result.
            # Normalization is NOT ADDED -- doing so would collapse two
            # events currently counted as distinct into one row.
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


register(InsiderTransactionsDataset())
