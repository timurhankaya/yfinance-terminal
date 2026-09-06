"""Cursors and query cost bounds."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from yfin.api.storage import cursor as cursors
from yfin.api.storage import limits

QUERY = {"route": "bars", "symbol": "AAPL", "interval": "1d"}


# --- cursors ----------------------------------------------------------------


def test_a_cursor_round_trips() -> None:
    key = (datetime(2026, 1, 2, 3, 4, tzinfo=UTC),)
    token = cursors.encode(key, query=QUERY)
    assert cursors.decode(token, query=QUERY, arity=1) == key


def test_a_date_key_keeps_its_type() -> None:
    key = (date(2026, 1, 2), "SPLIT")
    token = cursors.encode(key, query=QUERY)
    assert cursors.decode(token, query=QUERY, arity=2) == key


def test_a_cursor_from_ANOTHER_QUERY_is_refused() -> None:
    """A cursor from interval=1d handed to interval=1m would address a
    different table with a different key: either a 500 or a silently
    wrong and very expensive scan."""
    token = cursors.encode((1,), query=QUERY)
    with pytest.raises(cursors.InvalidCursor):
        cursors.decode(token, query={**QUERY, "interval": "1m"}, arity=1)


def test_a_cursor_from_an_OLDER_VERSION_is_refused() -> None:
    token = cursors.encode((1,), query=QUERY)
    original = cursors.CURSOR_VERSION
    try:
        cursors.CURSOR_VERSION = original + 1
        with pytest.raises(cursors.InvalidCursor):
            cursors.decode(token, query=QUERY, arity=1)
    finally:
        cursors.CURSOR_VERSION = original


def test_a_cursor_with_the_wrong_key_shape_is_refused() -> None:
    token = cursors.encode((1,), query=QUERY)
    with pytest.raises(cursors.InvalidCursor):
        cursors.decode(token, query=QUERY, arity=2)


@pytest.mark.parametrize("garbage", ["", "!!!!", "eyJhIjoxfQ", "not base64 at all"])
def test_garbage_raises_InvalidCursor_NEVER_something_else(garbage: str) -> None:
    """Every failure mode has to arrive as one exception type, or a
    malformed cursor reaches the query builder and becomes a 500."""
    with pytest.raises(cursors.InvalidCursor):
        cursors.decode(garbage, query=QUERY, arity=1)


def test_the_cursor_is_opaque_but_not_secret() -> None:
    """No signature: authorisation comes from the scope and the path, and
    a forged cursor only jumps within a query the caller could already
    make. What it must do is fail loudly when it does not fit."""
    token = cursors.encode(("AAPL",), query=QUERY)
    assert "AAPL" not in token


# --- query cost -------------------------------------------------------------


def test_each_interval_class_has_its_own_span_cap() -> None:
    assert limits.max_span("1m") < limits.max_span("1d") < limits.max_span("1mo")


def test_an_oversized_intraday_range_is_refused() -> None:
    """interval=1m&from=1980 scans a hypertable with millions of rows and
    holds a worker for minutes -- one request, well inside any per-second
    limit, because a rate limit bounds frequency and not cost."""
    start = datetime(1980, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 1, tzinfo=UTC)
    assert limits.window_error("1m", start, end) is not None


def test_a_range_within_the_cap_is_accepted() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    assert limits.window_error("1m", start, start + timedelta(days=30)) is None


def test_from_after_to_is_refused() -> None:
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    assert limits.window_error("1d", moment, moment - timedelta(days=1)) is not None


def test_an_open_ended_range_is_closed_with_the_cap() -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    start, end = limits.resolve_window(interval="1m", start=None, end=None, now=now)
    assert end == now
    assert end - start == limits.max_span("1m")


def test_only_from_given_extends_forward_by_the_cap() -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    begin = datetime(2020, 1, 1, tzinfo=UTC)
    start, end = limits.resolve_window(interval="1d", start=begin, end=None, now=now)
    assert start == begin
    assert end - start == limits.max_span("1d")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("AA", "AA"), ("A%", r"A\%"), ("A_B", r"A\_B"), ("A\\B", "A\\\\B")],
)
def test_like_wildcards_are_escaped(raw: str, expected: str) -> None:
    """A bare `%` would turn a prefix lookup into a full table scan:
    cheap to send, expensive to serve."""
    assert limits.escape_prefix(raw) == expected
