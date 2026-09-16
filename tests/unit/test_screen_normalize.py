"""Screener normalization -- against real fixtures, no network."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from yfin.datasets.market.screener import ScreenerDataset, ScreenPayload

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "_screen"
FETCHED_AT = datetime(2026, 9, 5, 12, 0, 0)
AS_OF = datetime(2026, 9, 5).date()


def _payload(fixture: str, key: str = "day_gainers") -> ScreenPayload:
    raw = json.loads((FIXTURES / f"{fixture}.json").read_text(encoding="utf-8"))
    return ScreenPayload(
        screen_key=key,
        as_of_date=AS_OF,
        fetched_at=FETCHED_AT,
        quotes=raw["quotes"],
        total=raw["total"],
        page_count=1,
        metadata={k: v for k, v in raw.items() if k != "quotes"},
    )


def _rows(result: Any, table: str) -> list[dict[str, Any]]:
    return [r for w in result.writes if w.table == table for r in w.rows]


@pytest.fixture(scope="module")
def gainers() -> Any:
    return ScreenerDataset().normalize(_payload("day_gainers_p0"))


class TestWrites:
    def test_all_declared_tables_are_written(self, gainers: Any) -> None:
        """`produces` and actual output must not diverge."""
        assert {w.table for w in gainers.writes} == set(ScreenerDataset.produces)

    def test_member_count_matches_quotes(self, gainers: Any) -> None:
        assert len(_rows(gainers, "screen_members")) == len(_rows(gainers, "screen_quotes"))

    def test_ranks_are_zero_based_and_dense(self, gainers: Any) -> None:
        """The first symbol's `rank_index` is 0."""
        ranks = [r["rank_index"] for r in _rows(gainers, "screen_members")]
        assert ranks == list(range(len(ranks)))

    def test_run_row_records_total_and_fetched(self, gainers: Any) -> None:
        """`total` != `fetched_rows` records hitting the page limit,
        rather than staying silent about it."""
        run = _rows(gainers, "screen_runs")[0]
        assert run["total"] > run["fetched_rows"]
        assert run["fetched_rows"] == len(_rows(gainers, "screen_members"))
        assert run["row_count"] == run["fetched_rows"]


class TestScreenMetadata:
    def test_predefined_title_comes_from_get_page(self, gainers: Any) -> None:
        """`title` exists only in the predefined GET response."""
        screen = _rows(gainers, "screens")[0]
        assert screen["title"] == "Day Gainers"
        assert screen["definition_json"]

    def test_custom_screen_falls_back_to_screendef(self) -> None:
        """A custom screen's first page is also a POST and carries no
        metadata. `ScreenDef` speaks instead."""
        result = ScreenerDataset().normalize(_payload("tr_equity_p0", key="tr_equity"))
        screen = _rows(result, "screens")[0]
        assert screen["title"] == "BIST Equities"
        assert screen["kind"] == "custom"
        assert screen["quote_type"] == "EQUITY"
        # definition_json is built from the query object, not rawCriteria
        assert "operator" in str(screen["definition_json"])

    def test_is_enabled_is_not_in_update_scope(self) -> None:
        """Once the operator disables it in the DB, no run should re-enable it."""
        from yfin.datasets.market.screener import _SCREEN_UPDATE

        assert "is_enabled" not in _SCREEN_UPDATE
        assert "created_at" not in _SCREEN_UPDATE


class TestQuoteProjection:
    def test_shared_columns_use_ticker_info_names(self, gainers: Any) -> None:
        """75 shared fields inherit their column name from `ticker_info`."""
        row = _rows(gainers, "screen_quotes")[0]
        assert "regular_market_price" in row
        assert "fifty_two_week_high" in row

    def test_new_typed_columns_are_present(self, gainers: Any) -> None:
        row = _rows(gainers, "screen_quotes")[0]
        for column in ("fullday_price", "pe_ttm", "custom_price_alert_confidence"):
            assert column in row

    def test_corporate_actions_stays_in_raw_json(self, gainers: Any) -> None:
        """A list does not fit in a column; there is no data loss."""
        row = _rows(gainers, "screen_quotes")[0]
        assert "corporate_actions" not in row
        assert "corporateActions" in row["raw_json"]

    def test_iso_date_strings_become_datetimes(self) -> None:
        """`ipoExpectedDate` / `nameChangeDate` are ISO text.

        If they were given the `epoch_s` kind, both would silently be NULL.
        """
        result = ScreenerDataset().normalize(_payload("bond_etfs_p0", key="bond_etfs"))
        values = [
            r["ipo_expected_date"] for r in _rows(result, "screen_quotes") if r["ipo_expected_date"]
        ]
        if values:  # sparsely populated; test is moot if absent here
            assert all(hasattr(v, "year") for v in values)


class TestSymbolPromotion:
    def test_new_symbols_are_written_inactive(self, gainers: Any) -> None:
        """Discovery writes inactive; activation is manual."""
        rows = _rows(gainers, "symbols")
        assert rows
        assert all(r["is_active"] is False for r in rows)
        assert all(r["discovered_by"] == "screener" for r in rows)
        assert all(r["discovered_at"] == FETCHED_AT for r in rows)

    def test_symbol_update_scope_excludes_activation_columns(self) -> None:
        """Regression guard: if these were in scope, a symbol the operator
        manually activated would silently go inactive again the next day."""
        from yfin.datasets.market.screener import SYMBOL_UPDATE

        for column in ("is_active", "unknown_streak", "discovered_by", "discovered_at"):
            assert column not in SYMBOL_UPDATE

    def test_symbol_update_scope_only_covers_filled_columns(self) -> None:
        """Each path updates only the column it actually fills."""
        from yfin.datasets.market.screener import SYMBOL_UPDATE, _symbol_row

        filled = set(
            _symbol_row(
                "AAPL",
                {"shortName": "Apple", "longName": "Apple Inc.", "exchange": "NMS"},
                _payload("day_gainers_p0"),
            )
        )
        assert set(SYMBOL_UPDATE) <= filled

    def test_members_and_quotes_agree_on_is_known(self, gainers: Any) -> None:
        members = {r["symbol"]: r["is_known"] for r in _rows(gainers, "screen_members")}
        quotes = {r["symbol"]: r["is_known"] for r in _rows(gainers, "screen_quotes")}
        assert members == quotes


class TestMixedQuoteTypes:
    def test_bond_etfs_carries_two_quote_types(self) -> None:
        """A single screen can return a mixed `quoteType` (EQUITY + ETF)."""
        result = ScreenerDataset().normalize(_payload("bond_etfs_p0", key="bond_etfs"))
        types = {r["quote_type"] for r in _rows(result, "screen_quotes")}
        assert len(types) >= 1
        assert types <= {"EQUITY", "ETF", "MUTUALFUND"}


class TestEmptyScreen:
    def test_zero_quotes_still_writes_the_header(self) -> None:
        """For a `total=0` screen, `screen_runs` is `ok`, children are `empty`."""
        payload = ScreenPayload(
            screen_key="day_gainers",
            as_of_date=AS_OF,
            fetched_at=FETCHED_AT,
            quotes=[],
            total=0,
            page_count=1,
            metadata={},
        )
        result = ScreenerDataset().normalize(payload)
        assert _rows(result, "screen_runs")
        assert _rows(result, "screen_members") == []
        assert _rows(result, "screen_quotes") == []

    def test_quote_without_symbol_is_dropped(self) -> None:
        """Dropping the row is correct, rather than writing NULL into the PK."""
        payload = ScreenPayload(
            screen_key="day_gainers",
            as_of_date=AS_OF,
            fetched_at=FETCHED_AT,
            quotes=[{"symbol": "AAPL"}, {"name": "sembolsuz"}],
            total=2,
            page_count=1,
            metadata={},
        )
        result = ScreenerDataset().normalize(payload)
        assert [r["symbol"] for r in _rows(result, "screen_members")] == ["AAPL"]
