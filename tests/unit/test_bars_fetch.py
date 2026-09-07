"""Bars fetch: what Yahoo's "no data" answers become, and how window
bounds are localised."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pandas as pd
import pytest
from yfinance import exceptions as yf_exceptions

from yfin.core.errors import ErrorKind, classify_error, is_no_data
from yfin.datasets.bars import IntervalBarDataset, _local_bound
from yfin.datasets.base import SyncContext


class _Ticker:
    """Records the history() calls; answers as told."""

    def __init__(self, tz: str | None, answer: Any) -> None:
        self._tz = tz
        self._answer = answer
        self.calls: list[dict[str, Any]] = []

    def _get_ticker_tz(self, timeout: int) -> str | None:
        return self._tz

    def get_history_metadata(self) -> dict[str, Any]:
        return {}

    def history(self, **kwargs: Any) -> pd.DataFrame:
        self.calls.append(kwargs)
        if isinstance(self._answer, BaseException):
            raise self._answer
        return self._answer


def _ctx(ticker: _Ticker, start: date, end: date) -> SyncContext:
    return SyncContext(
        symbol="AKGRT.IS",
        ticker=ticker,
        fetched_at=datetime(2026, 9, 7, 12, tzinfo=UTC),
        start=start,
        end=end,
    )


def test_a_symbol_yahoo_holds_no_bars_of_is_empty_not_failed() -> None:
    """Measured on the 2026-09-07 run: 88 of 355 5m/15m items failed with
    YFPricesMissingError for names Yahoo simply has no intraday data for.
    Every slice answered "nothing here" is an empty result."""
    missing = yf_exceptions.YFPricesMissingError(
        "AKGRT.IS", "15m data not available for startTime=1 and endTime=2"
    )
    ticker = _Ticker("Europe/Istanbul", missing)
    payload = IntervalBarDataset("15m").fetch(_ctx(ticker, date(2026, 8, 1), date(2026, 9, 1)))
    assert payload.frame.empty
    # The windows are still recorded, so a later run retries them.
    assert payload.failed_windows == ((date(2026, 8, 1), date(2026, 9, 1)),)
    assert len(ticker.calls) == 1


def test_a_real_failure_among_the_slices_still_raises() -> None:
    ticker = _Ticker("Europe/Istanbul", RuntimeError("connection reset"))
    with pytest.raises(RuntimeError, match="connection reset"):
        IntervalBarDataset("15m").fetch(_ctx(ticker, date(2026, 8, 1), date(2026, 9, 1)))


def test_no_data_exceptions_are_named_in_one_place() -> None:
    assert is_no_data(yf_exceptions.YFPricesMissingError("X", "none"))
    assert is_no_data(yf_exceptions.YFInvalidPeriodError("X", "max", ["1d"]))
    assert not is_no_data(RuntimeError("x"))
    assert classify_error(yf_exceptions.YFPricesMissingError("X", "none")) is ErrorKind.DATA


def test_window_bounds_are_localised_on_the_exchange_clock_across_a_dst_gap() -> None:
    """2024-09-08 00:00 does not exist in America/Santiago (clocks jump to
    01:00). yfinance's own localisation raised on it and every 60m fill
    of that range failed; the bound steps forward instead."""
    ticker = _Ticker("America/Santiago", pd.DataFrame())
    IntervalBarDataset("60m").fetch(_ctx(ticker, date(2024, 9, 8), date(2024, 9, 10)))
    assert len(ticker.calls) == 1
    start = ticker.calls[0]["start"]
    assert isinstance(start, pd.Timestamp)
    assert str(start.tz) == "America/Santiago"
    assert start.strftime("%Y-%m-%d %H:%M") == "2024-09-08 01:00"
    assert ticker.calls[0]["end"].strftime("%Y-%m-%d %H:%M") == "2024-09-10 00:00"


def test_without_a_known_zone_the_bound_stays_a_date_string() -> None:
    assert _local_bound(date(2024, 9, 8), None) == "2024-09-08"
    ticker = _Ticker(None, pd.DataFrame())
    IntervalBarDataset("60m").fetch(_ctx(ticker, date(2024, 9, 8), date(2024, 9, 10)))
    assert ticker.calls[0]["start"] == "2024-09-08"
