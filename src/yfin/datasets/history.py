"""history dataset -> price_history."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pandas as pd

from yfin.core import normalize as nz
from yfin.core.config import get_settings
from yfin.core.logging_setup import get_logger
from yfin.datasets.base import Dataset, NormalizedResult, SyncContext
from yfin.datasets.common import date_range_kwargs
from yfin.datasets.payloads import FramePayload
from yfin.datasets.registry import register
from yfin.ingest.client import call_yahoo
from yfin.storage.contracts import TableWrite

CACHE_HISTORY = "history_df"

log = get_logger(__name__)

# Measured once; unchanged for the process lifetime.
_REPAIR_AVAILABLE: bool | None = None

_ZERO = Decimal(0)

_COLUMN_MAP: dict[str, str] = {
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Adj Close": "adj_close",
    "Volume": "volume",
    "Dividends": "dividend",
    "Stock Splits": "split_ratio",
    "Capital Gains": "capital_gain",
}

# (dataset -> table, date column) mapping of consumers of the shared frame.
# `start` is the MINIMUM of their watermarks: if price_history is current but
# dividends is empty, a narrow incremental window would miss old dividends.
FRAME_CONSUMERS: dict[str, tuple[str, str]] = {
    "history": ("price_history", "session_date"),
    "dividends": ("dividends", "ex_date"),
    "splits": ("splits", "split_date"),
    "capital_gains": ("capital_gains", "gain_date"),
}

UPDATE_COLUMNS = (
    "ts_utc",
    "open",
    "high",
    "low",
    "close",
    "adj_close",
    "volume",
    "dividend",
    "split_ratio",
    "capital_gain",
    "is_repaired",
)

# is_repaired is never written BACK to False; it only moves 0 -> 1.
MONOTONIC_COLUMNS = ("is_repaired",)


def repair_enabled() -> bool:
    """Repair requested AND the [repair] extra installed?

    yfinance LAZILY imports scipy.ndimage and sklearn.cluster.DBSCAN in its
    repair heuristics (scrapers/history.py:820, 1338). If the extra is
    missing, the call fails with ModuleNotFoundError, which fails
    `history` + `dividends` + `splits` + `capital_gains` cells all at once
    for every symbol -- price_history never gets written at all. A working
    run without repair is preferred over a total data outage; the gap is
    logged once, VISIBLY.
    """
    if not get_settings().yf_history_repair:
        return False
    global _REPAIR_AVAILABLE
    if _REPAIR_AVAILABLE is None:
        try:
            import scipy.ndimage  # noqa: F401
            import sklearn.cluster  # noqa: F401

            _REPAIR_AVAILABLE = True
        except ImportError:
            _REPAIR_AVAILABLE = False
            log.error(
                "history repair devre disi: yfinance[repair] ekstrasi kurulu degil "
                "(pip install 'yfinance[repair]'); scipy ve scikit-learn gerekir"
            )
    return _REPAIR_AVAILABLE


def _shared_watermark(ctx: SyncContext) -> date | datetime | None:
    """Minimum watermark across the SELECTED tables consuming the frame.

    Returns None if any of them is empty (period="max"): that table needs
    its full history fetched.
    """
    names = ctx.selected if ctx.selected is not None else set(FRAME_CONSUMERS)
    marks: list[date | datetime] = []
    for name, (table, column) in FRAME_CONSUMERS.items():
        if name not in names:
            continue
        mark = ctx.watermark(table, column)
        if mark is None:
            return None
        marks.append(mark)
    if not marks:
        # No consumer is selected (direct call); falls back to
        # price_history, keeping behavior unchanged from before.
        return ctx.watermark("price_history", "session_date")
    return min(marks, key=_as_date)


def fetch_history_frame(ctx: SyncContext) -> pd.DataFrame:
    """Single daily series. capital_gains is NOT A SEPARATE NETWORK CALL -
    it comes from the same cache (history.py:723), shared via ctx.cached."""

    def _call() -> pd.DataFrame:
        kwargs: dict[str, Any] = {
            "interval": "1d",
            "auto_adjust": False,  # so 'Adj Close' comes as a separate column
            # actions=True is REQUIRED: dividends/splits/capital_gains are
            # fed from these columns (history.py:619-620, otherwise dropped).
            "actions": True,
            # Fixes Yahoo's known data errors (missing split/dividend
            # adjustment, 100x currency errors, duplicate dividends).
            # Reliable only at the 1d interval -- the one used here.
            "repair": repair_enabled(),
        }
        # --start/--end OVERRIDES the watermark: when the user explicitly
        # requests a range, the incremental window cannot narrow it.
        if ctx.start is not None or ctx.end is not None:
            kwargs.update(date_range_kwargs(ctx.start, ctx.end))
        else:
            watermark = _shared_watermark(ctx)
            if watermark is None:
                kwargs["period"] = "max"
            else:
                overlap = get_settings().yf_incremental_overlap_days
                start = _as_date(watermark) - timedelta(days=overlap)
                kwargs["start"] = start.isoformat()
        return call_yahoo(lambda: ctx.ticker.history(**kwargs), what=f"history:{ctx.symbol}")

    return ctx.cached(CACHE_HISTORY, _call)


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


class HistoryDataset(Dataset[FramePayload]):
    name = "history"
    depends_on = ("symbols",)
    produces = ("price_history",)
    # The range passes into the yfinance CALL -> a REAL backfill, not
    # "row filtering".
    date_range = "api"

    def fetch(self, ctx: SyncContext) -> FramePayload:
        return fetch_history_frame(ctx)

    def normalize(self, raw: FramePayload, symbol: str) -> NormalizedResult:
        if nz.is_empty_result(raw):
            return NormalizedResult()

        frame: pd.DataFrame = raw
        # Column set VARIES by symbol ('Capital Gains' is added for
        # fund/ETF); never relies on a fixed order or presence.
        present = {src: dst for src, dst in _COLUMN_MAP.items() if src in frame.columns}

        rows: list[dict[str, Any]] = []
        for index, record in zip(frame.index, frame.to_dict("records"), strict=True):
            session_date = nz.to_local_date(index)
            ts_utc = nz.to_datetime_utc(index)
            if session_date is None or ts_utc is None:
                continue
            close = nz.to_decimal(record.get("Close"))
            if close is None:
                # close is NOT NULL; a row with no close is meaningless.
                continue

            row: dict[str, Any] = {
                "symbol": symbol,
                "session_date": session_date,
                "ts_utc": ts_utc,
                "close": close,
                "dividend": _ZERO,
                "split_ratio": _ZERO,
                "capital_gain": _ZERO,
                # Kept in a FIXED dict, NOT put in _COLUMN_MAP: the generic
                # loop would process it with to_decimal in the `else` branch
                # and write a Decimal into a BOOLEAN column. Also, since it's
                # present in every row here, it never enters the `present`
                # intersection; if it did, a run where the column is absent
                # would also drop it from the ON DUPLICATE KEY UPDATE scope.
                "is_repaired": bool(nz.to_bool(record.get("Repaired?"))),
            }
            for src, dst in present.items():
                if dst == "close":
                    continue
                value = record.get(src)
                if dst == "volume":
                    volume = nz.to_int(value)
                    row[dst] = None if volume is None or volume < 0 else volume
                elif dst in ("dividend", "split_ratio", "capital_gain"):
                    row[dst] = nz.to_decimal(value) or _ZERO
                else:
                    row[dst] = nz.to_decimal(value)
            rows.append(row)

        if not rows:
            return NormalizedResult()

        return NormalizedResult(
            writes=[
                TableWrite(
                    table="price_history",
                    rows=rows,
                    key_columns=("symbol", "session_date"),
                    update_columns=UPDATE_COLUMNS,
                    monotonic_columns=MONOTONIC_COLUMNS,
                )
            ]
        )


register(HistoryDataset())
