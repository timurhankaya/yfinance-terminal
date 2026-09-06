"""Window planner tests.

This function is the first line of defense against data loss, and the
cheapest part to test since it needs no network or DB.

Measured limits: 1m allows 8 days per request / 30 days depth, 5m-15m 59,
60m 729. `BAR_LIMITS` holds these accepted values -- 9, 60, and 730 were
rejected -- so the margin is already baked into the constants and the
planner does not add a second margin on top.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from yfin.datasets.bars import BAR_LIMITS, plan_windows

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _span_days(window: tuple[object, object]) -> int:
    start, end = window
    return (end - start).days  # type: ignore[operator]


def test_first_fill_1m_is_four_slices_none_exceeding_the_request_limit() -> None:
    """30-day depth / 8-day window -> 4 slices."""
    plan = plan_windows("1m", None, NOW)

    assert len(plan.windows) == 4
    assert all(_span_days(w) <= 8 for w in plan.windows)
    assert plan.gap is None
    # Slices must be contiguous and cover the full depth
    assert plan.windows[0][0] == (NOW - timedelta(days=29)).date()
    assert plan.windows[-1][1] == NOW.date()
    for earlier, later in zip(plan.windows, plan.windows[1:], strict=False):
        assert earlier[1] == later[0], "slices are not contiguous - a day is lost"


@pytest.mark.parametrize("interval", ["5m", "15m"])
def test_first_fill_59_day_intervals_are_single_slice(interval: str) -> None:
    plan = plan_windows(interval, None, NOW)

    assert len(plan.windows) == 1
    assert _span_days(plan.windows[0]) == 59
    assert plan.gap is None


def test_first_fill_60m_is_single_729_day_slice() -> None:
    plan = plan_windows("60m", None, NOW)

    assert len(plan.windows) == 1
    assert _span_days(plan.windows[0]) == 729


@pytest.mark.parametrize("interval", ["1wk", "1mo"])
def test_first_fill_multiday_intervals_have_no_windows(interval: str) -> None:
    """For unbounded intervals, the first fill uses period='max'."""
    plan = plan_windows(interval, None, NOW)

    assert plan.windows == ()
    assert plan.gap is None


def test_normal_incremental_is_a_single_slice() -> None:
    watermark = NOW - timedelta(days=1)

    plan = plan_windows("1m", watermark, NOW, overlap_days=2)

    assert len(plan.windows) == 1
    assert plan.windows[0][0] == (watermark - timedelta(days=2)).date()
    assert plan.gap is None


def test_ten_day_pause_is_split_into_two_slices() -> None:
    """Exists because of a measured bug.

    A naive `start = watermark - overlap` asks for a 12-day window; Yahoo
    rejects it with YFPricesMissingError for exceeding the 8-day limit, and
    the WHOLE slice is lost. A pause would silently turn into data loss.
    """
    watermark = NOW - timedelta(days=10)

    plan = plan_windows("1m", watermark, NOW, overlap_days=2)

    assert len(plan.windows) == 2
    assert all(_span_days(w) <= 8 for w in plan.windows)
    assert plan.gap is None


def test_pause_beyond_retention_records_an_unrecoverable_gap() -> None:
    """35-day pause: the first 5 days are no longer available on Yahoo."""
    watermark = NOW - timedelta(days=35)

    plan = plan_windows("1m", watermark, NOW)

    assert plan.gap is not None
    gap_start, gap_end = plan.gap
    assert gap_start == watermark
    # The gap extends to the oldest fetchable point; no day in between
    # should go unrecorded
    assert gap_end.date() == plan.windows[0][0]
    assert all(_span_days(w) <= 8 for w in plan.windows)


def test_open_gaps_are_retried_while_still_inside_the_window() -> None:
    """bar_gaps is a work list, not just a tombstone.

    Once the watermark moves past a gap, the planner would never request it
    again on its own; an open gap must be re-sliced.
    """
    watermark = NOW - timedelta(days=1)
    gap = (NOW - timedelta(days=12), NOW - timedelta(days=11))

    plan = plan_windows("1m", watermark, NOW, overlap_days=2, open_gaps=[gap])

    covered = [w for w in plan.windows if w[0] <= gap[0].date() < w[1]]
    assert covered, f"open gap was not retried: {plan.windows}"


def test_open_gap_outside_retention_is_not_retried() -> None:
    """A gap whose window has closed must not generate a wasted request."""
    watermark = NOW - timedelta(days=1)
    gap = (NOW - timedelta(days=40), NOW - timedelta(days=39))

    plan = plan_windows("1m", watermark, NOW, overlap_days=2, open_gaps=[gap])

    assert all(w[0] >= (NOW - timedelta(days=29)).date() for w in plan.windows)


def test_no_window_ever_starts_at_the_retention_boundary() -> None:
    """Regression for a bug caught in a live run.

    BAR_LIMITS["1m"] used to be (8, 30); the planner started the oldest
    slice exactly at the limit and Yahoo returned YFPricesMissingError --
    AAPL's entire 1m first fill was dropped. Measured: -30d REJECTED,
    -29d 1950 bars.

    This test verifies that for every interval, the oldest slice stays at
    least one day inside Yahoo's announced limit.
    """
    announced = {"1m": 30, "5m": 60, "15m": 60, "60m": 730}
    for interval, limit in announced.items():
        plan = plan_windows(interval, None, NOW)
        oldest = plan.windows[0][0]
        assert oldest > (NOW - timedelta(days=limit)).date(), (
            f"{interval}: slice starts exactly at the limit, Yahoo will reject it"
        )


def test_explicit_range_overrides_watermark_and_produces_no_gap() -> None:
    """--start/--end is a manual backfill; exceeding depth is user error,
    not a "missed" fetch, and produces no gap."""
    watermark = NOW - timedelta(days=1)
    start = (NOW - timedelta(days=20)).date()
    end = (NOW - timedelta(days=5)).date()

    plan = plan_windows("1m", watermark, NOW, start=start, end=end)

    assert plan.gap is None
    assert plan.windows[0][0] == start
    assert plan.windows[-1][1] == end
    assert all(_span_days(w) <= 8 for w in plan.windows)


def test_explicit_range_ignores_open_gaps() -> None:
    watermark = NOW - timedelta(days=1)
    gap = (NOW - timedelta(days=12), NOW - timedelta(days=11))

    plan = plan_windows(
        "1m",
        watermark,
        NOW,
        start=(NOW - timedelta(days=3)).date(),
        end=NOW.date(),
        open_gaps=[gap],
    )

    assert len(plan.windows) == 1
    assert plan.windows[0][0] == (NOW - timedelta(days=3)).date()


def test_up_to_date_watermark_still_asks_for_the_overlap() -> None:
    """The overlap is idempotent and prevents a bar right at the session
    boundary from being missed; the window must not be empty even when
    the watermark is "now"."""
    plan = plan_windows("5m", NOW, NOW, overlap_days=2)

    assert len(plan.windows) == 1
    assert plan.windows[0][0] == (NOW - timedelta(days=2)).date()


def test_unknown_interval_is_rejected() -> None:
    with pytest.raises(ValueError, match="bilinmeyen interval"):
        plan_windows("3mo", None, NOW)


def test_bar_limits_match_the_measured_values() -> None:
    """Constants are the measured accepted values."""
    # 29, not 30: exactly 30 days was rejected in a live measurement (see bars.py).
    assert BAR_LIMITS["1m"] == (8, 29)
    assert BAR_LIMITS["5m"] == (59, 59)
    assert BAR_LIMITS["15m"] == (59, 59)
    assert BAR_LIMITS["60m"] == (729, 729)
    assert BAR_LIMITS["1wk"] == (None, None)
    assert BAR_LIMITS["1mo"] == (None, None)


def test_distant_open_gap_becomes_its_own_slice_not_a_giant_span() -> None:
    """A distant open gap must not force refetching every day in between.

    A naive approach slices from min(watermark, gap) to today: for a gap
    25 days back that produces 4 requests, three of which refetch data
    already written. The gap must become its own slice.
    """
    watermark = NOW - timedelta(days=1)
    gap = (NOW - timedelta(days=25), NOW - timedelta(days=24))

    plan = plan_windows("1m", watermark, NOW, overlap_days=2, open_gaps=[gap])

    assert len(plan.windows) == 2, f"gap should be its own slice: {plan.windows}"
    assert plan.windows[0][0] <= gap[0].date() < plan.windows[0][1]
    assert plan.windows[1][0] == (watermark - timedelta(days=2)).date()


def test_overlapping_gap_and_watermark_are_merged_into_one_slice() -> None:
    """A gap adjacent to the watermark window must not be split in two."""
    watermark = NOW - timedelta(days=3)
    gap = (NOW - timedelta(days=4), NOW - timedelta(days=3))

    plan = plan_windows("1m", watermark, NOW, overlap_days=2, open_gaps=[gap])

    assert len(plan.windows) == 1
