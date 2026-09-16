"""earnings_dates dataset.

Pagination needs a FRESH Ticker per page: yfinance caches `_earnings_dates`
by `limit` only, so a reused Ticker returns the first page for every offset.
"""

from __future__ import annotations

from functools import partial
from typing import Any

import pandas as pd

from yfin.core import normalize as nz
from yfin.core.config import get_settings
from yfin.core.families import DataFamily
from yfin.core.logging_setup import get_logger
from yfin.datasets.base import Dataset, NormalizedResult, SyncContext
from yfin.datasets.exposure import ApiExposure
from yfin.datasets.payloads import EarningsDatesPayload
from yfin.datasets.registry import register
from yfin.ingest.client import call_yahoo, make_ticker
from yfin.storage.contracts import TableWrite

log = get_logger(__name__)

# Yahoo caps the limit at 100 (base.py: ValueError).
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


def _fetch_pages(symbol: str, max_pages: int) -> tuple[pd.DataFrame | None, bool]:
    """The pages, and whether paging ran out of data rather than pages.

    The second value decides whether the result may REPLACE what is stored.
    """
    frames: list[pd.DataFrame] = []
    complete = False
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
            complete = True
            break
        frames.append(frame)
    if not complete:
        log.debug(
            "earnings dates hit the page cap; history may be truncated",
            symbol=symbol,
            max_pages=max_pages,
        )
    if not frames:
        return None, complete
    return pd.concat(frames), complete


class MissingTimezoneError(ValueError):
    """Source returned a tz-naive index; the timestamp cannot be interpreted."""


def _index_tz(index: Any) -> str:
    """Index tz; for an object-dtype index (fixtures) read from the first element.

    Never defaults to "UTC": a tz-naive index means the library changed,
    and a guessed tz would write shifted timestamps as if correct.
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
    api = (
        ApiExposure(
            family=DataFamily.FUNDAMENTALS,
            table="earnings_dates",
            sort_key=("earnings_ts_utc", "fact_hash"),
            descending=True,
            description="Past and upcoming earnings dates.",
        ),
    )

    def fetch(self, ctx: SyncContext) -> EarningsDatesPayload:
        max_pages = get_settings().yf_earnings_dates_max_pages
        frame, complete = _fetch_pages(ctx.symbol, max_pages)
        return EarningsDatesPayload(
            frame=frame, fetched_at=ctx.fetched_at, complete=complete
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
                log.debug("earnings date row has no timestamp", symbol=symbol)
                continue

            values = {
                "eps_estimate": nz.to_decimal(record.get("EPS Estimate")),
                "reported_eps": nz.to_decimal(record.get("Reported EPS")),
                "surprise_pct": nz.to_decimal(record.get("Surprise(%)")),
            }
            # fact_hash feeds the PK: two rows on one timestamp can differ only
            # in Surprise(%) with both EPS fields NaN. Canonical JSON turns NaN
            # into null, so they are stringified to keep the hashes distinct.
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
            # Pages can overlap: without dedup on the PK,
            # rows_verified < rows_attempted would wrongly produce `failed`.
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
                    # Replace, not merge: `fact_hash` is in the key, so an
                    # estimate turning into a reported result would otherwise
                    # leave the superseded row behind. Only when the fetch
                    # reached the end of the history.
                    mode="replace_scope" if raw.complete else "upsert",
                )
            ]
        )


register(EarningsDatesDataset())
