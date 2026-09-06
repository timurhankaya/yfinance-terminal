"""price_bars normalization and is_extended.

Runs against real fixtures; no network, no database. Each symbol is proof
of its own edge case -- measured distribution in the fixtures:

    AAPL      849 bars, 522 extended   (hasPrePost=True)
    SHEL.L    503 bars,   5 extended   (hasPrePost=FALSE)
    VWCE.DE   504 bars,   8 extended   (hasPrePost=FALSE)
    THYAO.IS  479 bars,   0            (pre/post degenerate)
    BTC-USD  1322 bars,   0            (24/7)
    GC=F     1232 bars,   0            (bar opens at 18:10)
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest

from helpers import as_frame, load_fixture
from yfin.datasets.bars import BarPayload, normalize_bars


def _payload(symbol: str, dataset: str = "bars_5m", interval: str = "5m") -> BarPayload:
    raw = load_fixture(symbol, dataset)
    periods = raw.get("trading_periods")
    return BarPayload(
        frame=as_frame(raw["frame"]),
        trading_periods=as_frame(periods) if periods else None,
        interval=interval,
    )


def _rows(symbol: str, dataset: str = "bars_5m", interval: str = "5m") -> list[dict[str, Any]]:
    result = normalize_bars(_payload(symbol, dataset, interval), symbol)
    assert len(result.writes) >= 1
    return result.writes[0].rows


# --- is_extended ----------------------------------------------------------


@pytest.mark.parametrize(
    ("symbol", "expected_extended"),
    [("AAPL", 522), ("SHEL.L", 5), ("VWCE.DE", 8), ("THYAO.IS", 0), ("BTC-USD", 0), ("GC=F", 0)],
)
def test_extended_bar_counts_match_the_measured_fixtures(
    symbol: str, expected_extended: int
) -> None:
    rows = _rows(symbol)
    assert sum(1 for r in rows if r["is_extended"]) == expected_extended


def test_shell_l_regression_extended_bars_despite_has_prepost_false() -> None:
    """Prevents a removed early exit from coming back.

    SHEL.L reports hasPrePostMarketData=False but still returns 16:30/16:35
    bars (regular session is 08:00-16:30). An early exit based on
    has_pre_post_market_data would count these as regular session and let
    them into v_price_bars_regular -- exactly the corruption that view
    exists to prevent.
    """
    rows = _rows("SHEL.L")
    extended = [r for r in rows if r["is_extended"]]

    assert extended, "may have exited early on hasPrePost=False"
    assert all(r["ts_utc"].time() >= datetime(2026, 1, 1, 15, 30).time() for r in extended)


def test_thyao_degenerate_pre_post_columns_do_not_mark_everything_extended() -> None:
    """For THYAO, the pre_*/post_* columns degenerate, not start/end.

    tradingPeriods: pre=09:30-09:30, reg=09:30-18:00, post=18:00-18:00.
    The rule only looks at start/end, so all bars land in the regular
    session.
    """
    rows = _rows("THYAO.IS")

    assert not any(r["is_extended"] for r in rows)
    assert len(rows) == 479


def test_multiday_intervals_carry_no_extended_column() -> None:
    """The concept is meaningless for 1wk/1mo.

    These rows used to carry `is_extended=False`. Now they carry no such
    column at all: bars above daily go to `periodic_bars`, which has no
    such column. Instead of "fill a meaningless field with False", the
    field is not created -- the concept does not exist in the schema either.
    """
    for dataset, interval in (("bars_1wk", "1wk"), ("bars_1mo", "1mo")):
        rows = _rows("AAPL", dataset, interval)
        assert rows
        assert all("is_extended" not in r for r in rows)


def test_missing_trading_periods_defaults_to_not_extended() -> None:
    """The safe default is 0: counting an unknown bar as extended would hide
    it from v_price_bars_regular; the opposite failure is more visible."""
    payload = BarPayload(frame=_payload("AAPL").frame, trading_periods=None, interval="5m")

    rows = normalize_bars(payload, "AAPL").writes[0].rows

    assert not any(r["is_extended"] for r in rows)


def test_trading_periods_without_pre_post_columns_is_accepted() -> None:
    """tradingPeriods fetched with prepost=False carries only start/end;
    code accessing pre_start would raise a KeyError."""
    payload = _payload("SHEL.L")
    assert payload.trading_periods is not None
    trimmed = payload.trading_periods[["start", "end"]]

    rows = (
        normalize_bars(
            BarPayload(frame=payload.frame, trading_periods=trimmed, interval="5m"), "SHEL.L"
        )
        .writes[0]
        .rows
    )

    assert sum(1 for r in rows if r["is_extended"]) == 5


# --- normalization -------------------------------------------------------


def test_local_date_is_the_local_calendar_day_not_the_utc_day() -> None:
    """GC=F's session opens in the evening and crosses UTC midnight.

    Since America/New_York is -04:00, a local 20:00 bar falls on the next
    UTC day. Deriving local_date from ts_utc would shift that bar's day
    forward by one; the source index already carries the local tz, so the
    date is taken from there instead.
    """
    rows = _rows("GC=F")
    shifted = [r for r in rows if r["local_date"] != r["ts_utc"].date()]

    assert shifted, "no bar crossing the UTC day found - unexpected fixture"
    for row in shifted:
        # UTC day must be exactly one ahead of the local day
        assert (row["ts_utc"].date() - row["local_date"]).days == 1
        assert row["ts_utc"].hour < 5


def test_ts_utc_is_naive_utc() -> None:
    """Column is timestamptz(6); conversion happens in normalize and returns UTC-aware."""
    rows = _rows("AAPL")

    assert all(r["ts_utc"].tzinfo is UTC for r in rows)
    assert isinstance(rows[0]["local_date"], date)


def test_write_targets_price_bars_with_the_three_column_key() -> None:
    result = normalize_bars(_payload("AAPL"), "AAPL")
    write = result.writes[0]

    assert write.table == "price_bars"
    assert write.key_columns == ("symbol", "bar_interval", "ts_utc")
    assert "is_extended" in write.update_columns
    assert write.monotonic_columns == ()


def test_derived_action_columns_are_not_written() -> None:
    """dividend/split_ratio/capital_gain are derived; the authoritative
    tables are dividends, splits, capital_gains. adj_close is also not
    written: it goes stale in a permanent archive."""
    rows = _rows("AAPL")

    for banned in ("dividend", "split_ratio", "capital_gain", "adj_close", "is_repaired"):
        assert banned not in rows[0], f"{banned} must not be written to price_bars"


def test_rows_carry_the_interval() -> None:
    rows = _rows("BTC-USD")

    assert {r["bar_interval"] for r in rows} == {"5m"}
    assert {r["symbol"] for r in rows} == {"BTC-USD"}


def test_row_without_close_is_dropped() -> None:
    """close is NOT NULL; a bar without a close is meaningless."""
    payload = _payload("AAPL")
    frame = payload.frame.copy()
    frame.iloc[0, frame.columns.get_loc("Close")] = None

    rows = (
        normalize_bars(
            BarPayload(frame=frame, trading_periods=payload.trading_periods, interval="5m"), "AAPL"
        )
        .writes[0]
        .rows
    )

    assert len(rows) == len(payload.frame) - 1


def test_negative_volume_becomes_null() -> None:
    payload = _payload("AAPL")
    frame = payload.frame.copy()
    frame.iloc[0, frame.columns.get_loc("Volume")] = -5

    rows = (
        normalize_bars(
            BarPayload(frame=frame, trading_periods=payload.trading_periods, interval="5m"), "AAPL"
        )
        .writes[0]
        .rows
    )

    assert rows[0]["volume"] is None


def test_prices_are_decimal_not_float() -> None:
    rows = _rows("AAPL")

    assert isinstance(rows[0]["close"], Decimal)


def test_empty_frame_yields_empty_result() -> None:
    payload = BarPayload(frame=pd.DataFrame(), trading_periods=None, interval="5m")

    assert normalize_bars(payload, "AAPL").is_empty


def test_unknown_interval_is_rejected() -> None:
    payload = _payload("AAPL")

    with pytest.raises(ValueError, match="unknown interval"):
        normalize_bars(
            BarPayload(frame=payload.frame, trading_periods=None, interval="3mo"), "AAPL"
        )
