"""The predefined screen set is derived from the library, not hand-written.

Same reasoning as the rule for the `domain_key` set: don't silently drift
the day the library adds or removes a screen. A hand-written list would
only reveal that once someone noticed.
"""

from __future__ import annotations

from yfinance import PREDEFINED_SCREENER_QUERIES

from yfin.ingest.screens import PREDEFINED_SCREENS, ScreenDef


def test_predefined_keys_match_library() -> None:
    assert {s.key for s in PREDEFINED_SCREENS} == set(PREDEFINED_SCREENER_QUERIES)


def test_predefined_sort_matches_library() -> None:
    """`sort_field` / `sort_asc` exactly match the library's definition.

    Sort order is given explicitly. Deviating from the library's order
    would make our roster differ from Yahoo's own screen, and
    `screen_members.rank` would measure something else.
    """
    for screen in PREDEFINED_SCREENS:
        spec = PREDEFINED_SCREENER_QUERIES[screen.key]
        assert screen.sort_field == spec["sortField"]
        assert screen.sort_asc == (spec["sortType"].lower() == "asc")


def test_predefined_quote_type_matches_query_class() -> None:
    """quote_type is derived from the library's query class."""
    expected = {
        "EquityQuery": "EQUITY",
        "FundQuery": "MUTUALFUND",
        "ETFQuery": "ETF",
    }
    for screen in PREDEFINED_SCREENS:
        cls = type(PREDEFINED_SCREENER_QUERIES[screen.key]["query"]).__name__
        assert screen.quote_type == expected[cls]


def test_predefined_carries_no_query_object() -> None:
    """For predefined, `query` is None: the name is enough, and the first
    page fetches metadata via GET. Keeping a query object would fall to
    the POST path and `title`/`rawCriteria` would never be obtained."""
    for screen in PREDEFINED_SCREENS:
        assert screen.query is None
        assert screen.kind == "predefined"


def test_screendef_is_frozen() -> None:
    screen = PREDEFINED_SCREENS[0]
    assert isinstance(screen, ScreenDef)
    try:
        screen.key = "x"  # type: ignore[misc]
    except AttributeError:
        return
    raise AssertionError("ScreenDef must be frozen")
