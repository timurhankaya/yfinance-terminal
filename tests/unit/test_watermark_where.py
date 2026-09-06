"""SyncContext.watermark's `where` parameter.

A single MAX(ts_utc) on price_bars gives the wrong answer: 1m can be
up-to-date while 60m is two years behind. Without splitting per interval,
bars_60m would be assumed "current" and its first fill would never run.

Existing calls (history, shares_full) pass no `where` and their behavior
must not change; this file verifies both.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

from yfin.datasets.base import SyncContext


class RecordingProvider:
    """Fake provider that records watermark calls."""

    def __init__(self, answers: dict[tuple[str, str, str, str], date | datetime] | None = None):
        self.calls: list[tuple[str, str, str, Mapping[str, Any] | None]] = []
        self.answers = answers or {}

    def __call__(
        self,
        table: str,
        column: str,
        symbol: str,
        *,
        where: Mapping[str, Any] | None = None,
    ) -> date | datetime | None:
        self.calls.append((table, column, symbol, where))
        key = (table, column, symbol, str(where))
        return self.answers.get(key)


def _ctx(provider: RecordingProvider) -> SyncContext:
    return SyncContext(
        "AAPL",
        object(),
        datetime(2026, 9, 4, tzinfo=UTC),
        watermark_provider=provider,
    )


def test_where_reaches_the_provider() -> None:
    provider = RecordingProvider()
    ctx = _ctx(provider)

    ctx.watermark("price_bars", "ts_utc", where={"bar_interval": "60m"})

    assert provider.calls == [("price_bars", "ts_utc", "AAPL", {"bar_interval": "60m"})]


def test_existing_calls_pass_no_where() -> None:
    """The history/shares_full pattern: no `where` is given, it passes as None."""
    provider = RecordingProvider()
    ctx = _ctx(provider)

    ctx.watermark("price_history", "session_date")

    assert provider.calls == [("price_history", "session_date", "AAPL", None)]


def test_intervals_are_isolated_from_each_other() -> None:
    """Why this test exists: without the interval dimension, 60m's
    watermark gets confused with 1m's and the first fill never runs."""
    provider = RecordingProvider(
        {
            ("price_bars", "ts_utc", "AAPL", "{'bar_interval': '1m'}"): datetime(2026, 9, 3),
            # No record for 60m -> None -> first fill
        }
    )
    ctx = _ctx(provider)

    assert ctx.watermark("price_bars", "ts_utc", where={"bar_interval": "1m"}) == datetime(
        2026, 9, 3
    )
    assert ctx.watermark("price_bars", "ts_utc", where={"bar_interval": "60m"}) is None


def test_full_refresh_still_short_circuits_with_where() -> None:
    """--full-refresh skips watermarks; `where` must not change that."""
    provider = RecordingProvider()
    ctx = SyncContext(
        "AAPL",
        object(),
        datetime(2026, 9, 4, tzinfo=UTC),
        watermark_provider=provider,
        full_refresh=True,
    )

    assert ctx.watermark("price_bars", "ts_utc", where={"bar_interval": "1m"}) is None
    assert provider.calls == []
