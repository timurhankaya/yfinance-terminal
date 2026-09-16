"""Live Yahoo verifications -- Search / Lookup / Screener (`-m live`).

Tests assert a relationship, not a number: counts move during the day.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import yfinance as yf

from yfin.datasets.base import SyncContext
from yfin.datasets.discovery.lookup import ALL_TYPE, TYPED_LOOKUPS, LookupDataset, _fetch_type
from yfin.datasets.discovery.search import SearchDataset
from yfin.datasets.market.base import MarketContext
from yfin.datasets.market.screener import ScreenerDataset
from yfin.ingest.screens import screen_by_key

pytestmark = pytest.mark.live

NOW = datetime.now(UTC)


def _ctx(term: str) -> SyncContext:
    return SyncContext(symbol=term, ticker=None, fetched_at=NOW)


def _mctx(variant: str) -> MarketContext:
    return MarketContext(
        fetched_at=NOW, start=NOW.date(), end=NOW.date()
    ).for_variant(variant)


# --- Lookup: two sides of the same behavior --------------------------------
#
# Both tests are required together: the narrow-term case alone would lock in
# a wrong invariant.


def test_narrow_term_all_equals_typed_union() -> None:
    """Narrow term: `all` equals the full typed union, zero diff both ways."""
    term = "AAPL"
    block = _fetch_type(term, ALL_TYPE, 1000)
    all_symbols = {d["symbol"] for d in block["documents"] if "symbol" in d}
    assert block["lookupTotals"]["all"] <= 500, "AAPL should be a narrow term"

    union: set[str] = set()
    for lookup_type in TYPED_LOOKUPS:
        typed = _fetch_type(term, lookup_type, 1000)
        union |= {d["symbol"] for d in typed.get("documents") or [] if "symbol" in d}

    assert all_symbols - union == set()
    assert union - all_symbols == set()


def test_broad_term_all_is_truncated_and_typed_union_is_wider() -> None:
    """Broad term: `all` is truncated around 1000, typed union is far wider.

    If this goes red, the adaptive branch it protects should be removed.
    """
    term = "GOLD"
    block = _fetch_type(term, ALL_TYPE, 1000)
    all_symbols = {d["symbol"] for d in block["documents"] if "symbol" in d}
    reported = block["lookupTotals"]["all"]

    assert reported > 1000, "GOLD should be a broad term"
    assert len(all_symbols) < reported, "`all` should be truncated"

    union: set[str] = set()
    for lookup_type in TYPED_LOOKUPS:
        typed = _fetch_type(term, lookup_type, 1000)
        union |= {d["symbol"] for d in typed.get("documents") or [] if "symbol" in d}

    assert len(union) > len(all_symbols) * 2, (len(union), len(all_symbols))


def test_lookup_dataset_writes_totals_for_nine_types() -> None:
    """`privateCompany` isn't in the `LOOKUP_TYPES` constant; read from the response."""
    payload = LookupDataset().fetch(_ctx("BTC"))
    assert "privateCompany" in payload.totals
    assert len(payload.totals) >= 9


# --- Search: non-default flags ---------------------------------------------


def test_research_requires_an_explicit_flag() -> None:
    """`include_research` defaults to False; a completeness check can't tell
    "source returned empty" from "we didn't ask".
    """
    default = yf.Search("AAPL").response
    explicit = yf.Search("AAPL", include_research=True).response
    assert not default.get("researchReports")
    assert explicit.get("researchReports")


def test_search_quotes_carry_symbolless_rows() -> None:
    """`include_cb=True` default pulls in Crunchbase records with no symbol.

    A blind `q["symbol"]` would KeyError. More visible on a broad term.
    """
    raw = yf.Search("gold", max_results=10).response
    quotes = raw.get("quotes") or []
    assert quotes
    assert any("symbol" not in q for q in quotes), "expected a symbolless row"


def test_search_dataset_drops_symbolless_rows() -> None:
    payload = SearchDataset().fetch(_ctx("AAPL"))
    result = SearchDataset().normalize(payload, "AAPL")
    rows = [r for w in result.writes if w.table == "search_quotes" for r in w.rows]
    assert rows
    assert all(r["symbol"] for r in rows)
    assert [r["rank_index"] for r in rows] == list(range(len(rows)))


# --- Screener: paging and ordering ------------------------------------------


def test_paging_needs_size_not_count() -> None:
    """When `offset` is given, `count` is silently ignored."""
    with_count = yf.screen("top_mutual_funds", offset=250, count=250)
    with_size = yf.screen("top_mutual_funds", offset=250, size=250)
    assert len(with_count["quotes"]) < len(with_size["quotes"])
    assert len(with_size["quotes"]) == 250


def test_offset_beyond_total_returns_empty_without_error() -> None:
    """Third branch of the stop condition: no error, 0 rows."""
    page = yf.screen("day_gainers", offset=9000, size=25)
    assert page["quotes"] == []


def test_custom_screen_first_page_carries_no_metadata() -> None:
    """A custom screen's first page is also a POST: 5 keys, no `title`."""
    spec = screen_by_key("tr_equity")
    assert spec.query is not None
    page = yf.screen(spec.query, size=5, sortField=spec.sort_field, sortAsc=spec.sort_asc)
    assert "title" not in page
    assert set(page) <= {"count", "quotes", "start", "total", "useRecords"}


def test_predefined_first_page_carries_metadata() -> None:
    page = yf.screen("day_gainers", count=5)
    assert page["title"]
    assert "rawCriteria" in page


def test_screener_dataset_paginates_to_total() -> None:
    """Four pages must fetch the whole `tr_equity` total."""
    from yfin.core.config import get_settings

    cfg = get_settings().model_copy(update={"yf_screen_max_pages": 4, "yf_screen_size": 250})
    import yfin.core.config as config_mod

    original = config_mod.get_settings
    config_mod.get_settings = lambda: cfg  # type: ignore[assignment]
    try:
        payload = ScreenerDataset().fetch(_mctx("tr_equity"))
    finally:
        config_mod.get_settings = original  # type: ignore[assignment]

    assert payload.total > 500
    assert len(payload.quotes) == payload.total
    symbols = [q["symbol"] for q in payload.quotes]
    assert symbols == sorted(symbols), "sortAsc=True should give a stable ascending order"
    assert len(set(symbols)) == len(symbols), "pages must not overlap"
