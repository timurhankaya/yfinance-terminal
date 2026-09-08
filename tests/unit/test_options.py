"""Normalization of the options dataset. No network, no database.

The two tables answer two questions and are checked as two: the expiry
list is complete because it comes free with the first request, while the
chain is fetched for the first `yf_option_expiries` only.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from yfin.datasets import SYMBOL_DATASETS
from yfin.datasets.base import NormalizedResult
from yfin.datasets.options import EXPIRATIONS_TABLE, QUOTES_TABLE, OptionsPayload
from yfin.datasets.registry import SYMBOL_DATASETS as REGISTRY
from yfin.models.options import OptionType
from yfin.storage.contracts import TableWrite

NOW = datetime(2026, 9, 8, 21, 0, tzinfo=UTC)
AS_OF = date(2026, 9, 8)


def _rows(result: NormalizedResult, table: str) -> list[dict[str, Any]]:
    return [row for write in result.writes if write.table == table for row in write.rows]


def _write(result: NormalizedResult, table: str) -> TableWrite:
    return next(write for write in result.writes if write.table == table)


def _chain(**over: Any) -> pd.DataFrame:
    """One contract, in the fourteen columns `_options2df` forces."""
    row: dict[str, Any] = {
        "contractSymbol": "AAPL260918C00250000",
        "lastTradeDate": pd.Timestamp("2026-09-05 19:58:12", tz="UTC"),
        "strike": 250.0,
        "lastPrice": 12.35,
        "bid": 12.30,
        "ask": 12.40,
        "change": -0.65,
        "percentChange": -4.99,
        "volume": 1841,
        "openInterest": 20567,
        "impliedVolatility": 0.2673,
        "inTheMoney": True,
        "contractSize": "REGULAR",
        "currency": "USD",
    }
    row.update(over)
    return pd.DataFrame([row])


def _payload(**over: Any) -> OptionsPayload:
    defaults: dict[str, Any] = {
        "expiries": ["2026-09-11", "2026-09-18", "2026-09-25"],
        "chains": [("2026-09-11", _chain(), _chain(contractSymbol="AAPL260911P00250000"))],
        "fetched_at": NOW,
    }
    defaults.update(over)
    return OptionsPayload(**defaults)


@pytest.fixture
def dataset() -> Any:
    return SYMBOL_DATASETS["options"]


# --- registration ----------------------------------------------------------


def test_it_is_opt_in_so_a_run_never_picks_it_up_by_itself() -> None:
    """One request per expiry per symbol against a universe that already
    takes ~33 hours: this must never join the `all` expansion."""
    assert REGISTRY.is_opt_in("options")
    assert "options" not in REGISTRY.resolve(None)


def test_it_is_selectable_by_name() -> None:
    assert [ds.name for ds in REGISTRY.resolve(["options"])][-1] == "options"


# --- the expiry list -------------------------------------------------------


def test_every_expiry_is_recorded_not_only_the_fetched_ones(dataset: Any) -> None:
    """The list comes free with the first request, so it is complete even
    though only one chain was fetched. Nothing else in the archive
    remembers an expiry once it drops off."""
    result = dataset.normalize(_payload(), "AAPL")
    rows = _rows(result, EXPIRATIONS_TABLE)
    assert [row["expiry_date"] for row in rows] == [
        date(2026, 9, 11),
        date(2026, 9, 18),
        date(2026, 9, 25),
    ]
    assert {row["symbol"] for row in rows} == {"AAPL"}
    assert {row["as_of_date"] for row in rows} == {AS_OF}


def test_the_expiry_scope_is_the_whole_day(dataset: Any) -> None:
    """Stated rather than read off the rows: an expiry that dropped off
    the list has to lose its row, and a scope derived from the rows could
    never remove it."""
    write = _write(dataset.normalize(_payload(), "AAPL"), EXPIRATIONS_TABLE)
    assert write.mode == "replace_scope"
    assert write.scope_columns == ("symbol", "as_of_date")
    assert write.scope_values == ({"symbol": "AAPL", "as_of_date": AS_OF},)


def test_an_unparsable_expiry_is_dropped_rather_than_failing_the_symbol(
    dataset: Any,
) -> None:
    result = dataset.normalize(_payload(expiries=["2026-09-11", "not a date"]), "AAPL")
    assert [row["expiry_date"] for row in _rows(result, EXPIRATIONS_TABLE)] == [date(2026, 9, 11)]


# --- the chain -------------------------------------------------------------


def test_calls_and_puts_share_a_table_and_are_told_apart_by_an_enum(dataset: Any) -> None:
    """Their column sets are identical by construction -- yfinance builds
    both frames with the same reindex -- which is the case this codebase
    answers with one table plus a discriminator."""
    rows = _rows(dataset.normalize(_payload(), "AAPL"), QUOTES_TABLE)
    assert sorted(row["option_type"] for row in rows) == [
        OptionType.CALL.value,
        OptionType.PUT.value,
    ]


def test_every_column_yahoo_sends_is_mapped(dataset: Any) -> None:
    result = dataset.normalize(_payload(chains=[("2026-09-11", _chain(), None)]), "AAPL")
    rows = _rows(result, QUOTES_TABLE)
    assert rows == [
        {
            "symbol": "AAPL",
            "as_of_date": AS_OF,
            "expiry_date": date(2026, 9, 11),
            "option_type": OptionType.CALL.value,
            "contract_symbol": "AAPL260918C00250000",
            "strike": Decimal("250"),
            "last_price": Decimal("12.35"),
            "bid": Decimal("12.3"),
            "ask": Decimal("12.4"),
            "change": Decimal("-0.65"),
            "percent_change": Decimal("-4.99"),
            "implied_volatility": Decimal("0.2673"),
            "volume": 1841,
            "open_interest": 20567,
            "in_the_money": True,
            "contract_size": "REGULAR",
            "currency": "USD",
            "last_trade_ts_utc": datetime(2026, 9, 5, 19, 58, 12, tzinfo=UTC),
            "fetched_at": NOW,
        }
    ]


def test_a_contract_with_no_strike_is_dropped_not_written(dataset: Any) -> None:
    """`strike` is NOT NULL, and one malformed entry must not roll back
    the whole symbol's transaction."""
    result = dataset.normalize(
        _payload(chains=[("2026-09-11", _chain(strike=None), None)]), "AAPL"
    )
    assert _rows(result, QUOTES_TABLE) == []


def test_the_quote_scope_is_only_the_expiries_actually_fetched(dataset: Any) -> None:
    """A chain the run did not ask for keeps yesterday's rows rather than
    being emptied by a run that never looked at it."""
    write = _write(dataset.normalize(_payload(), "AAPL"), QUOTES_TABLE)
    assert write.mode == "replace_scope"
    assert write.scope_columns == ("symbol", "as_of_date", "expiry_date")
    assert write.scope_values == (
        {"symbol": "AAPL", "as_of_date": AS_OF, "expiry_date": date(2026, 9, 11)},
    )


def test_an_expiry_whose_chain_came_back_empty_still_clears_its_scope(dataset: Any) -> None:
    """The expiry WAS fetched, and it having no contracts today is data."""
    result = dataset.normalize(_payload(chains=[("2026-09-11", None, None)]), "AAPL")
    write = _write(result, QUOTES_TABLE)
    assert write.rows == []
    assert write.scope_values == (
        {"symbol": "AAPL", "as_of_date": AS_OF, "expiry_date": date(2026, 9, 11)},
    )


# --- absence ---------------------------------------------------------------


def test_a_symbol_with_no_options_writes_nothing_and_deletes_nothing(dataset: Any) -> None:
    """`call_optional` cannot tell a transient 404 from a symbol that
    genuinely has no options, and a replace_scope here would destroy the
    day's rows on a blip -- the rule `institutional_holders` writes down."""
    assert dataset.normalize(_payload(expiries=[], chains=[]), "AAPL").writes == []


def test_the_list_is_written_even_when_no_chain_was(dataset: Any) -> None:
    """The expiries are the gate's source table, so they have to survive a
    day where every chain came back empty."""
    result = dataset.normalize(_payload(chains=[]), "AAPL")
    assert len(_rows(result, EXPIRATIONS_TABLE)) == 3
    assert [write.table for write in result.writes] == [EXPIRATIONS_TABLE]


# --- what it costs ---------------------------------------------------------


class _FakeTicker:
    """Counts what was asked for. The expiry list is one request and each
    chain is another; that asymmetry is the whole cost model."""

    def __init__(self, expiries: list[str]) -> None:
        self.options = tuple(expiries)
        self.asked: list[str] = []

    def option_chain(self, date: str) -> Any:
        self.asked.append(date)
        return SimpleNamespace(calls=_chain(), puts=None, underlying={})


def _context(ticker: _FakeTicker) -> Any:
    return SimpleNamespace(symbol="AAPL", ticker=ticker, fetched_at=NOW)


def test_it_fetches_no_more_chains_than_the_setting_allows(
    dataset: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cost lives here: a symbol has ten to twenty expiries and each
    one is a request, so the bound is what keeps this dataset from
    multiplying a run by fifteen."""
    from yfin.datasets import options as module

    monkeypatch.setattr(
        module, "get_settings", lambda: SimpleNamespace(yf_option_expiries=2)
    )
    ticker = _FakeTicker(["2026-09-11", "2026-09-18", "2026-09-25", "2026-10-02"])
    payload = dataset.fetch(_context(ticker))
    assert ticker.asked == ["2026-09-11", "2026-09-18"]
    # ...but every expiry is still recorded, because the list was free.
    assert len(payload.expiries) == 4
    assert len(payload.chains) == 2


def test_a_symbol_with_no_expiries_costs_exactly_one_request(
    dataset: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from yfin.datasets import options as module

    monkeypatch.setattr(
        module, "get_settings", lambda: SimpleNamespace(yf_option_expiries=4)
    )
    ticker = _FakeTicker([])
    payload = dataset.fetch(_context(ticker))
    assert ticker.asked == []
    assert payload.expiries == [] and payload.chains == []
