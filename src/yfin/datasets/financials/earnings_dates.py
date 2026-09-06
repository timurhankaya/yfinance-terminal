"""earnings_dates dataset.

Pagination needs a FRESH Ticker: `TickerBase._earnings_dates`'s dict is keyed
only on `limit` (`base.py:637`); `offset` never enters the cache key. Reusing
the same Ticker with offset=100 returns the first page itself (`a is b` ->
True) and pagination silently no-ops. This is the second exception to the
ctx.cached rule, after `news`.
"""

from __future__ import annotations

from functools import partial
from typing import Any

import pandas as pd

from yfin.core import normalize as nz
from yfin.core.config import get_settings
from yfin.core.logging_setup import get_logger
from yfin.datasets.base import Dataset, NormalizedResult, SyncContext
from yfin.datasets.payloads import EarningsDatesPayload
from yfin.datasets.registry import register
from yfin.ingest.client import call_yahoo, make_ticker
from yfin.storage.contracts import TableWrite

log = get_logger(__name__)

# Yahoo caps the limit at 100 (base.py:634: ValueError).
PAGE_LIMIT = 100
KEY_COLUMNS = ("symbol", "earnings_ts_utc", "fact_hash")
UPDATE_COLUMNS = (
    "earnings_date_local",
    "tz_name",
    "eps_estimate",
    "reported_eps",
    "surprise_pct",
    "fetched_at",
)


def _fetch_pages(symbol: str, max_pages: int) -> pd.DataFrame | None:
    frames: list[pd.DataFrame] = []
    for page in range(max_pages):
        offset = page * PAGE_LIMIT
        # Fresh Ticker per page; otherwise the cache ignores offset.
        ticker = make_ticker(symbol)
        frame = call_yahoo(
            partial(ticker.get_earnings_dates, limit=PAGE_LIMIT, offset=offset),
            what=f"earnings_dates:{symbol}:{offset}",
        )
        # Stop condition is an EMPTY PAGE, not len(page) < limit.
        if nz.is_empty_result(frame):
            break
        frames.append(frame)
    if not frames:
        return None
    return pd.concat(frames)


class MissingTimezoneError(ValueError):
    """Source returned a tz-naive index; the timestamp cannot be interpreted."""


def _index_tz(index: Any) -> str:
    """Index tz; for an object-dtype index (fixtures) read from the first element.

    Never DEFAULTS to "UTC" when tz is missing. Measured 100% of symbols
    tz-aware, returning `America/New_York` (including THYAO.IS and 7203.T);
    a tz-naive index means the library's behavior changed. Assuming "UTC"
    would shift both `tz_name` and `earnings_ts_utc` by 4-5 hours and write
    wrong data as if correct -- a silent corruption. The cell becomes
    `failed` instead (wrong data is worse than missing data).
    """
    tz = getattr(index, "tz", None)
    if tz is None and len(index):
        tz = getattr(index[0], "tz", None)
    if tz is None:
        raise MissingTimezoneError(
            "the earnings_dates index carries no tz; the stamp cannot be interpreted "
            "(every symbol measured returned America/New_York)"
        )
    return str(tz)


class EarningsDatesDataset(Dataset[EarningsDatesPayload]):
    name = "earnings_dates"
    depends_on = ("symbols",)
    produces = ("earnings_dates",)

    def fetch(self, ctx: SyncContext) -> EarningsDatesPayload:
        max_pages = get_settings().yf_earnings_dates_max_pages
        return EarningsDatesPayload(
            frame=_fetch_pages(ctx.symbol, max_pages), fetched_at=ctx.fetched_at
        )

    def normalize(self, raw: EarningsDatesPayload, symbol: str) -> NormalizedResult:
        frame = raw.frame
        if nz.is_empty_result(frame):
            return NormalizedResult()
        assert isinstance(frame, pd.DataFrame)

        tz_name = _index_tz(frame.index)
        rows: dict[tuple[Any, ...], dict[str, Any]] = {}

        for index, record in frame.iterrows():
            ts_utc = nz.to_datetime_utc(index)
            local_date = nz.to_local_date(index)
            if ts_utc is None or local_date is None:
                log.warning("earnings date row has no timestamp", symbol=symbol)
                continue

            values = {
                "eps_estimate": nz.to_decimal(record.get("EPS Estimate")),
                "reported_eps": nz.to_decimal(record.get("Reported EPS")),
                "surprise_pct": nz.to_decimal(record.get("Surprise(%)")),
            }
            # fact_hash feeds the PK: for AAPL's 2002-07-16 timestamp, two rows
            # differ ONLY in Surprise(%); both EPS fields are NaN. Canonical
            # JSON turns NaN into null, so without this the two rows would hash
            # identically and one would silently disappear.
            digest = nz.content_hash(
                {k: (str(v) if v is not None else None) for k, v in values.items()}
            )
            row = {
                "symbol": symbol,
                "earnings_ts_utc": ts_utc,
                "fact_hash": digest[:16],
                "earnings_date_local": local_date,
                "tz_name": tz_name,
                **values,
                "fetched_at": raw.fetched_at,
            }
            # Overlap across pages was measured (1 row): without dedup on the
            # PK, rows_verified < rows_attempted would wrongly produce `failed`.
            rows[(ts_utc, row["fact_hash"])] = row

        if not rows:
            return NormalizedResult()
        return NormalizedResult(
            writes=[
                TableWrite(
                    table="earnings_dates",
                    rows=list(rows.values()),
                    key_columns=KEY_COLUMNS,
                    update_columns=UPDATE_COLUMNS,
                )
            ]
        )


register(EarningsDatesDataset())
