"""bar_gaps's three flows.

These were noticed during an audit: the table was being written, but only
for 'retention_expired'. Two flows were missing, and their absence was
silent:

  * `fetch_failed` was never written -> the planner's open_gaps mechanism
    was reading from a table that would never fill: dead code.
  * `resolved_at` was never populated -> a gap, once written, stays open
    forever and gets refetched for nothing on every run.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pandas as pd

from yfin.datasets.bars import BarPayload, normalize_bars


def _payload(**kwargs: Any) -> BarPayload:
    defaults: dict[str, Any] = {
        "frame": pd.DataFrame(),
        "trading_periods": None,
        "interval": "1m",
    }
    defaults.update(kwargs)
    return BarPayload(**defaults)


def _gap_rows(result: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for write in result.writes:
        if write.table == "bar_gaps":
            rows.extend(write.rows)
    return rows


def test_retention_gap_is_recorded_as_a_permanent_loss() -> None:
    payload = _payload(gap=(datetime(2026, 8, 1), datetime(2026, 8, 6)))

    rows = _gap_rows(normalize_bars(payload, "AAPL"))

    assert len(rows) == 1
    assert rows[0]["reason"] == "retention_expired"
    # This is a loss record, not a task: it must not be retried
    assert rows[0]["resolved_at"] is None


def test_failed_window_is_recorded_as_a_retryable_task() -> None:
    """The window may still be open; the planner retries it."""
    payload = _payload(failed_windows=((date(2026, 9, 1), date(2026, 9, 3)),))

    rows = _gap_rows(normalize_bars(payload, "AAPL"))

    assert len(rows) == 1
    assert rows[0]["reason"] == "fetch_failed"
    assert rows[0]["resolved_at"] is None
    assert rows[0]["gap_start_utc"] == datetime(2026, 9, 1)


def test_successful_window_closes_the_open_gap_inside_it() -> None:
    """Without this, the gap would stay open forever."""
    payload = _payload(
        fetched_windows=((date(2026, 9, 1), date(2026, 9, 5)),),
        open_gaps=((datetime(2026, 9, 2), datetime(2026, 9, 3)),),
    )

    rows = _gap_rows(normalize_bars(payload, "AAPL"))

    assert len(rows) == 1
    assert rows[0]["resolved_at"] is not None


def test_open_gap_outside_the_fetched_window_stays_open() -> None:
    payload = _payload(
        fetched_windows=((date(2026, 9, 1), date(2026, 9, 5)),),
        open_gaps=((datetime(2026, 8, 20), datetime(2026, 8, 21)),),
    )

    assert _gap_rows(normalize_bars(payload, "AAPL")) == []


def test_resolution_write_only_touches_resolved_at() -> None:
    """The closing write does not update `reason`: changing a row's reason
    would corrupt the audit trail."""
    payload = _payload(
        fetched_windows=((date(2026, 9, 1), date(2026, 9, 5)),),
        open_gaps=((datetime(2026, 9, 2), datetime(2026, 9, 3)),),
    )

    write = next(w for w in normalize_bars(payload, "AAPL").writes if w.table == "bar_gaps")

    assert write.update_columns == ("resolved_at",)


def test_recording_write_never_reopens_a_resolved_gap() -> None:
    """The `fetch_failed` write never updates resolved_at: reopening a
    closed gap would refetch it on every run."""
    payload = _payload(failed_windows=((date(2026, 9, 1), date(2026, 9, 3)),))

    write = next(w for w in normalize_bars(payload, "AAPL").writes if w.table == "bar_gaps")

    assert "resolved_at" not in write.update_columns


def test_no_gap_no_write() -> None:
    assert normalize_bars(_payload(), "AAPL").writes == []
