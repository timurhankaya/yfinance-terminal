"""funds_data dataset. No network, no database.

`ctx.cached` prefetch keys are filled in by hand in the test, so no real
HTTP call is made and it becomes visible that the precheck reads values
left behind by the `symbols` bootstrap.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd
from yfinance.exceptions import YFDataException

from yfin.datasets import SYMBOL_DATASETS
from yfin.datasets.base import NormalizedResult, SyncContext, TableWrite
from yfin.datasets.payloads import FundsPayload
from yfin.datasets.symbols import CACHE_FAST_INFO, CACHE_HISTORY_METADATA

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
AS_OF = date(2026, 9, 4)
DATASET = SYMBOL_DATASETS["funds_data"]


def _rows(result: NormalizedResult, table: str) -> list[dict[str, Any]]:
    return [row for write in result.writes if write.table == table for row in write.rows]


def _write(result: NormalizedResult, table: str) -> TableWrite:
    return next(write for write in result.writes if write.table == table)


class FakeFundsData:
    """Shape of the real `FundsData`: `quote_type` is a method, the rest are not.

    In yfinance 1.7.0, nine of ten fields carry `@property`, but
    `quote_type` does not (`scrapers/funds.py:47`) -- verified with a live
    measurement. This fake carries that inconsistency, otherwise the test
    would pass while production code writes "<bound method ...>" into a
    NOT NULL column.
    """

    def __init__(self, *, sectors: dict[str, float], ratings: dict[str, float], holdings: int):
        self.description = "Fon aciklamasi"
        self.fund_overview = {
            "categoryName": "Large Blend",
            "family": "SPDR State Street",
            "legalType": None,
        }
        self.fund_operations = pd.DataFrame(
            {
                "SPY": [0.0945, 0.02, 6.5e11],
                "Category Average": [0.5, 0.4, 2.0e10],
            },
            index=pd.Index(
                ["Annual Report Expense Ratio", "Annual Holdings Turnover", "Total Net Assets"],
                name="Attributes",
            ),
        )
        self.asset_classes = {
            "cashPosition": 0.01,
            "stockPosition": 0.99,
            "bondPosition": 0.0,
            "preferredPosition": 0.0,
            "convertiblePosition": 0.0,
            "otherPosition": 0.0,
        }
        self.top_holdings = pd.DataFrame(
            {
                "Name": [f"Holding {i}" for i in range(holdings)],
                "Holding Percent": [0.07 - i * 0.001 for i in range(holdings)],
            },
            index=pd.Index([f"H{i}" for i in range(holdings)], name="Symbol"),
        )
        self.equity_holdings = pd.DataFrame(
            {
                "SPY": [25.0, 4.5, 3.1, 15.0, 1.0e11, 0.12],
                "Category Average": [24.0, 4.0, 3.0, 14.0, 9.0e10, 0.10],
            },
            index=pd.Index(
                [
                    "Price/Earnings",
                    "Price/Book",
                    "Price/Sales",
                    "Price/Cashflow",
                    "Median Market Cap",
                    "3 Year Earnings Growth",
                ],
                name="Average",
            ),
        )
        self.bond_holdings = pd.DataFrame(
            {"SPY": [6.1, 8.4, None], "Category Average": [6.0, 8.0, None]},
            index=pd.Index(["Duration", "Maturity", "Credit Quality"], name="Average"),
        )
        self.bond_ratings = ratings
        self.sector_weightings = sectors

    def quote_type(self) -> str:
        return "ETF"


class FakeTicker:
    def __init__(self, funds: Any = None, *, error: BaseException | None = None) -> None:
        self.funds = funds
        self.error = error
        self.calls = 0

    def get_funds_data(self) -> Any:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.funds


def _context(ticker: FakeTicker, quote_type: str | None, instrument_type: str | None = None):
    ctx = SyncContext("SPY", ticker, NOW)
    # Values left in the ctx cache by the `symbols` bootstrap dataset
    ctx.cached(CACHE_FAST_INFO, lambda: ({"quoteType": quote_type} if quote_type else {}))
    ctx.cached(CACHE_HISTORY_METADATA, lambda: {"instrumentType": instrument_type})
    return ctx


def _equity_fund() -> FakeFundsData:
    """Measured from SPY/QQQ/VFIAX: 11 sectors + 1 rating, 10 holdings."""
    return FakeFundsData(
        sectors={f"sector_{i}": 0.09 for i in range(11)},
        ratings={"aaa": 1.0},
        holdings=10,
    )


def _bond_fund() -> FakeFundsData:
    """Measured from BND/TLT: 0 sectors + 9 ratings, top_holdings empty."""
    return FakeFundsData(
        sectors={},
        ratings={"us_government": 0.0, "aaa": 0.4, "below_b": 0.01},
        holdings=0,
    )


# --- precheck ----------------------------------------------------------------


def test_non_fund_symbol_makes_no_request_at_all() -> None:
    """For a non-fund symbol, no request is made at all; the cell is `empty`."""
    ticker = FakeTicker(_equity_fund())
    payload = DATASET.fetch(_context(ticker, "EQUITY"))

    assert payload.data is None
    assert ticker.calls == 0
    assert DATASET.normalize(payload, "AAPL").is_empty


def test_precheck_falls_back_to_instrument_type() -> None:
    """Without `fast_info['quoteType']`, falls back to
    `history_metadata['instrumentType']`; both come from the same chart
    request, so there is no extra cost."""
    ticker = FakeTicker(_equity_fund())
    payload = DATASET.fetch(_context(ticker, None, "ETF"))

    assert payload.data is not None
    assert ticker.calls == 1


def test_precheck_reads_camelcase_key() -> None:
    """The key is `quoteType`; `quote_type` (snake_case) returns None for
    every symbol, and the precheck would silently request every symbol."""
    ticker = FakeTicker(_equity_fund())
    ctx = SyncContext("SPY", ticker, NOW)
    ctx.cached(CACHE_FAST_INFO, lambda: {"quote_type": "ETF"})
    ctx.cached(CACHE_HISTORY_METADATA, lambda: {})

    assert DATASET.fetch(ctx).data is None


def test_raw_key_error_is_absorbed_as_empty() -> None:
    """Under hide_exceptions=False, the source raises a raw
    KeyError('topHoldings'), not YFDataException (scrapers/funds.py:190-194)."""
    ticker = FakeTicker(error=KeyError("topHoldings"))
    assert DATASET.fetch(_context(ticker, "ETF")).data is None


def test_yf_data_exception_is_absorbed_as_empty() -> None:
    ticker = FakeTicker(error=YFDataException("no fund data"))
    assert DATASET.fetch(_context(ticker, "ETF")).data is None


# --- normalization -----------------------------------------------------------


def test_equity_fund_writes_four_tables() -> None:
    result = DATASET.normalize(FundsPayload(_collect(_equity_fund()), NOW), "SPY")

    assert len(_rows(result, "fund_profile")) == 1
    assert len(_rows(result, "fund_weightings")) == 12  # 11 sectors + 1 rating
    assert len(_rows(result, "fund_top_holdings")) == 10
    # 6 equity + 3 bond: the metric set is fixed, and a row with a NULL
    # value is still written ("metric exists, no value") -- same decision
    # as analyst_estimates. The source builds a 3-row bond frame even for
    # an equity fund.
    assert len(_rows(result, "fund_metrics")) == 9


def test_bond_fund_has_no_sector_weights_and_no_holdings() -> None:
    """BND/TLT: 0 sectors + 9 ratings; `fund_top_holdings` is empty --
    that table is `empty`, its siblings are not."""
    result = DATASET.normalize(FundsPayload(_collect(_bond_fund()), NOW), "BND")

    weights = _rows(result, "fund_weightings")
    assert {row["category"] for row in weights} == {"bond_rating"}
    assert _rows(result, "fund_top_holdings") == []


def test_bond_rating_zero_is_a_real_weight() -> None:
    """`us_government = 0.0` is a real value, not NULL."""
    result = DATASET.normalize(FundsPayload(_collect(_bond_fund()), NOW), "BND")
    weights = {row["item_key"]: row["weight"] for row in _rows(result, "fund_weightings")}
    assert weights["us_government"] == Decimal("0")


def test_fund_metrics_keep_section_in_key() -> None:
    """`section` is part of the PK: without it, the same `metric` name
    appearing in two sections would raise ERROR 1062."""
    result = DATASET.normalize(FundsPayload(_collect(_equity_fund()), NOW), "SPY")
    write = _write(result, "fund_metrics")
    assert write.key_columns == ("symbol", "as_of_date", "section", "metric")
    metrics = {(row["section"], row["metric"]) for row in write.rows}
    assert ("equity", "price_to_earnings") in metrics
    assert ("bond", "duration") in metrics


def test_fund_operations_first_column_is_read_by_position() -> None:
    """Column 0's name is the symbol itself; it cannot be read by name."""
    result = DATASET.normalize(FundsPayload(_collect(_equity_fund()), NOW), "SPY")
    row = _rows(result, "fund_profile")[0]
    assert row["expense_ratio"] == Decimal("0.0945")
    assert row["expense_ratio_cat"] == Decimal("0.5")


def test_asset_classes_are_typed_columns_not_eav() -> None:
    """The six keys were measured as fixed across all 10 of 10 funds."""
    result = DATASET.normalize(FundsPayload(_collect(_equity_fund()), NOW), "SPY")
    row = _rows(result, "fund_profile")[0]
    assert row["stock_position"] == Decimal("0.99")
    assert row["bond_position"] == Decimal("0")


def test_profile_carries_raw_json() -> None:
    """The body is kept because category info can be lost when reducing to
    an EAV; raw_json exists ONLY in this table."""
    result = DATASET.normalize(FundsPayload(_collect(_equity_fund()), NOW), "SPY")
    row = _rows(result, "fund_profile")[0]
    assert '"sector_weightings"' in row["raw_json"]


def test_top_holdings_are_ranked_and_scoped() -> None:
    result = DATASET.normalize(FundsPayload(_collect(_equity_fund()), NOW), "SPY")
    write = _write(result, "fund_top_holdings")
    # Cannot be named `rank`: reserved as a window function
    assert [row["holding_rank"] for row in write.rows] == list(range(10))
    assert write.scope_values == ({"symbol": "SPY", "as_of_date": AS_OF},)


def test_is_known_defaults_to_false_and_is_filled_at_upsert() -> None:
    """`holding_symbol` has no FK: the source includes symbols outside the
    universe (2330.TW, 0700.HK, even fund symbols like VRTPX). With an FK,
    the fund's entire write would roll back."""
    result = DATASET.normalize(FundsPayload(_collect(_equity_fund()), NOW), "SPY")
    assert all(row["is_known"] is False for row in _rows(result, "fund_top_holdings"))


def test_quote_type_is_read_as_a_method_not_an_attribute() -> None:
    """A blind `funds.quote_type` access would write a bound method (live measurement)."""
    data = _collect(_equity_fund())
    assert data["quote_type"] == "ETF"


def test_property_form_of_quote_type_also_works() -> None:
    """If the upstream inconsistency gets fixed, the read must not break."""
    from yfin.datasets.funds import _read

    class Fixed:
        @property
        def quote_type(self) -> str:
            return "MUTUALFUND"

    assert _read(Fixed(), "quote_type") == "MUTUALFUND"


def test_partial_object_without_quote_type_writes_nothing() -> None:
    """For a non-fund symbol, a second access to `description` returns a
    company summary instead; a partially populated object cannot be trusted."""
    data = _collect(_equity_fund())
    data["quote_type"] = None
    assert DATASET.normalize(FundsPayload(data, NOW), "AAPL").is_empty


def _collect(funds: FakeFundsData) -> dict[str, Any]:
    from yfin.datasets.funds import _collect as collect

    return collect(funds)
