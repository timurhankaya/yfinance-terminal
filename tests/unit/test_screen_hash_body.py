"""`screen_runs.content_hash` covers only the roster, not the quotes: a moving
`regularMarketPrice` in the hash body would mean it never matches, and the gate would
silently die while looking correct."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from yfin.datasets.market.screener import ScreenerDataset, ScreenPayload

FETCHED_AT = datetime(2026, 9, 5, 12, 0, 0)


def _payload(quotes: list[dict[str, Any]], *, fetched_at: datetime = FETCHED_AT) -> ScreenPayload:
    return ScreenPayload(
        screen_key="day_gainers",
        as_of_date=datetime(2026, 9, 5).date(),
        fetched_at=fetched_at,
        quotes=quotes,
        total=len(quotes),
        page_count=1,
        metadata={},
    )


def _hash(quotes: list[dict[str, Any]], **kw: Any) -> str:
    result = ScreenerDataset().normalize(_payload(quotes, **kw))
    row = next(r for w in result.writes if w.table == "screen_runs" for r in w.rows)
    return str(row["content_hash"])


def test_price_change_does_not_change_hash() -> None:
    """Regression guard: a price move does not change the roster."""
    a = [{"symbol": "AAPL", "regularMarketPrice": 100.0, "marketCap": 1}]
    b = [{"symbol": "AAPL", "regularMarketPrice": 271.5, "marketCap": 9}]
    assert _hash(a) == _hash(b)


def test_fetched_at_does_not_change_hash() -> None:
    quotes = [{"symbol": "AAPL"}]
    assert _hash(quotes) == _hash(quotes, fetched_at=datetime(2026, 9, 6, 3, 0, 0))


def test_membership_change_changes_hash() -> None:
    assert _hash([{"symbol": "AAPL"}]) != _hash([{"symbol": "MSFT"}])


def test_added_member_changes_hash() -> None:
    assert _hash([{"symbol": "AAPL"}]) != _hash([{"symbol": "AAPL"}, {"symbol": "MSFT"}])


def test_rank_change_changes_hash() -> None:
    """`rank_index` is in the hash body and must be: when the roster stays
    the same but the order changes, that is a real change. Leaving it out
    would mean "AAPL is #1 today" never gets written."""
    first = _hash([{"symbol": "AAPL"}, {"symbol": "MSFT"}])
    second = _hash([{"symbol": "MSFT"}, {"symbol": "AAPL"}])
    assert first != second


def test_quotes_are_still_written_when_hash_matches() -> None:
    """`screen_quotes` is still written even when the hash matches.

    The gate only skips `screen_members`; a quote is independent of the
    screen, and the prices have changed.
    """
    result = ScreenerDataset().normalize(_payload([{"symbol": "AAPL", "regularMarketPrice": 1.0}]))
    tables = {w.table for w in result.writes if w.rows}
    assert "screen_quotes" in tables
    assert "screen_members" in tables
