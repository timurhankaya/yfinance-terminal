"""Screen definitions validated with no network: `EquityQuery`/`FundQuery`/`ETFQuery` raise
ValueError for an invalid field or value at module load."""

from __future__ import annotations

import pytest

from yfin.ingest.screens import (
    ALL_SCREENS,
    CUSTOM_SCREENS,
    PREDEFINED_SCREENS,
    SCREEN_KEY_MAX_LENGTH,
    screen_by_key,
)


def test_all_screens_is_union() -> None:
    assert len(ALL_SCREENS) == len(PREDEFINED_SCREENS) + len(CUSTOM_SCREENS)


def test_keys_are_unique() -> None:
    keys = [s.key for s in ALL_SCREENS]
    assert len(keys) == len(set(keys))


def test_keys_fit_sync_run_items_symbol_column() -> None:
    """The limit comes from `sync_run_items.symbol` = VARCHAR(32) COLLATE "C"."""
    for screen in ALL_SCREENS:
        assert len(screen.key) <= SCREEN_KEY_MAX_LENGTH, screen.key
        assert screen.key.isascii(), screen.key


def test_custom_screens_carry_a_query() -> None:
    """A query object is required for custom: there's no name, so the POST body is built from it."""
    for screen in CUSTOM_SCREENS:
        assert screen.kind == "custom"
        assert screen.query is not None


def test_custom_queries_are_constructible() -> None:
    """Being constructible already means they were validated client-side.
    This also proves the POST body can be built via `to_dict()`."""
    for screen in CUSTOM_SCREENS:
        assert screen.query is not None
        body = screen.query.to_dict()
        assert "operator" in body
        assert "operands" in body


def test_every_screen_declares_sort() -> None:
    """`sortAsc`'s default is descending; if order is not stable across
    pages, pages overlap or a symbol gets skipped."""
    for screen in ALL_SCREENS:
        assert screen.sort_field
        assert isinstance(screen.sort_asc, bool)


def test_quote_type_is_known() -> None:
    for screen in ALL_SCREENS:
        assert screen.quote_type in {"EQUITY", "MUTUALFUND", "ETF"}


def test_titles_are_present() -> None:
    """`screens.title` is written NOT NULL; for predefined it's refreshed
    from the first GET page, but the seed value cannot be empty."""
    for screen in ALL_SCREENS:
        assert screen.title.strip()


def test_screen_by_key_roundtrip() -> None:
    for screen in ALL_SCREENS:
        assert screen_by_key(screen.key) is screen


def test_screen_by_key_rejects_unknown() -> None:
    with pytest.raises(KeyError):
        screen_by_key("nosuchscreen")


def test_tr_equity_custom_screen_exists() -> None:
    """The only custom entry for BIST discovery."""
    screen = screen_by_key("tr_equity")
    assert screen.kind == "custom"
    assert screen.quote_type == "EQUITY"
    assert screen.sort_asc is True
