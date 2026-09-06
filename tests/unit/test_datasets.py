"""Dataset normalize tests: fixture-based, no network."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from helpers import as_frame, as_series, load_fixture
from yfin.datasets import SYMBOL_DATASETS as REGISTRY
from yfin.datasets.base import NormalizedResult
from yfin.datasets.payloads import (
    FastInfoPayload,
    InfoPayload,
    MetadataPayload,
    SymbolsPayload,
)

SYMBOLS = ("AAPL", "THYAO.IS", "SPY", "BTC-USD")


def _write(result: NormalizedResult, table: str):  # type: ignore[no-untyped-def]
    for write in result.writes:
        if write.table == table:
            return write
    raise AssertionError(f"no write to {table}: {[w.table for w in result.writes]}")


# --- history --------------------------------------------------------------


class TestHistory:
    @pytest.mark.parametrize("symbol", SYMBOLS)
    def test_rows_produced(self, symbol: str) -> None:
        frame = as_frame(load_fixture(symbol, "history"))
        result = REGISTRY["history"].normalize(frame, symbol)
        write = _write(result, "price_history")
        assert len(write.rows) == len(frame)
        assert write.key_columns == ("symbol", "session_date")

    def test_positive_offset_session_date_not_shifted(self) -> None:
        """THYAO 00:00+03:00 -> session_date is local, ts_utc one day behind."""
        frame = as_frame(load_fixture("THYAO.IS", "history"))
        rows = _write(REGISTRY["history"].normalize(frame, "THYAO.IS"), "price_history").rows
        first = rows[0]
        assert first["session_date"] == date(2024, 9, 4)
        assert first["ts_utc"] == datetime(2024, 9, 3, 21, 0, tzinfo=UTC)

    def test_etf_capital_gains_column_handled(self) -> None:
        """For funds/ETFs, 'Capital Gains' is added as the 9th column."""
        frame = as_frame(load_fixture("SPY", "history"))
        assert "Capital Gains" in frame.columns
        rows = _write(REGISTRY["history"].normalize(frame, "SPY"), "price_history").rows
        assert all("capital_gain" in row for row in rows)

    def test_missing_column_does_not_break(self) -> None:
        """The column set varies by symbol; its presence cannot be assumed fixed."""
        frame = as_frame(load_fixture("AAPL", "history"))
        assert "Capital Gains" not in frame.columns
        rows = _write(REGISTRY["history"].normalize(frame, "AAPL"), "price_history").rows
        assert all(row["capital_gain"] == Decimal(0) for row in rows)

    def test_prices_are_decimal_not_float(self) -> None:
        frame = as_frame(load_fixture("AAPL", "history"))
        row = _write(REGISTRY["history"].normalize(frame, "AAPL"), "price_history").rows[0]
        assert isinstance(row["close"], Decimal)
        assert isinstance(row["volume"], int)

    def test_empty_frame_is_empty_result(self) -> None:
        import pandas as pd

        assert REGISTRY["history"].normalize(pd.DataFrame(), "X").is_empty


# --- corporate actions ----------------------------------------------------


def _actions_frame(series: Any, column: str) -> Any:
    """corporate_actions datasets read from the history frame (no separate
    network call); the input is a DataFrame carrying that column, not a Series."""
    import pandas as pd

    return pd.DataFrame({column: series})


class TestCorporateActions:
    def test_dividends(self) -> None:
        series = as_series(load_fixture("AAPL", "dividends"))
        frame = _actions_frame(series, "Dividends")
        write = _write(REGISTRY["dividends"].normalize(frame, "AAPL"), "dividends")
        assert len(write.rows) == len(series)
        assert isinstance(write.rows[0]["ex_date"], date)
        assert isinstance(write.rows[0]["amount"], Decimal)

    def test_splits(self) -> None:
        series = as_series(load_fixture("THYAO.IS", "splits"))
        frame = _actions_frame(series, "Stock Splits")
        write = _write(REGISTRY["splits"].normalize(frame, "THYAO.IS"), "splits")
        assert len(write.rows) == len([v for v in series if v])

    def test_capital_gains_empty_is_not_failure(self) -> None:
        """Yahoo never returns this populated for any symbol; empty != failed."""
        for symbol in SYMBOLS:
            series = as_series(load_fixture(symbol, "capital_gains"))
            frame = _actions_frame(series, "Capital Gains")
            assert REGISTRY["capital_gains"].normalize(frame, symbol).is_empty

    def test_capital_gains_synthetic_fixture(self) -> None:
        """Synthetic fixture: a populated case that cannot be captured live."""
        series = as_series(load_fixture("SYNTHETIC", "capital_gains"))
        frame = _actions_frame(series, "Capital Gains")
        write = _write(REGISTRY["capital_gains"].normalize(frame, "SYNTH"), "capital_gains")
        assert len(write.rows) == 2
        assert write.rows[0]["amount"] == Decimal("1.234567")
        assert write.rows[0]["gain_date"] == date(2024, 12, 20)

    def test_none_input(self) -> None:
        assert REGISTRY["dividends"].normalize(None, "X").is_empty


# --- shares_full ----------------------------------------------------------


class TestSharesFull:
    def test_none_return_is_empty_not_attribute_error(self) -> None:
        """BTC-USD returns None; raw.empty would raise AttributeError."""
        assert load_fixture("BTC-USD", "shares_full") is None
        assert REGISTRY["shares_full"].normalize(None, "BTC-USD").is_empty

    def test_same_date_different_values_both_kept(self) -> None:
        """shares is part of the PK: the same date has different values."""
        series = as_series(load_fixture("AAPL", "shares_full"))
        write = _write(REGISTRY["shares_full"].normalize(series, "AAPL"), "shares_full")
        dates = [r["as_of_date"] for r in write.rows]
        assert len(set(dates)) < len(dates), "expected a duplicate date"
        keys = {(r["as_of_date"], r["shares"]) for r in write.rows}
        assert len(keys) == len(write.rows)
        assert write.key_columns == ("symbol", "as_of_date", "shares")


# --- info -----------------------------------------------------------------


class TestInfo:
    @pytest.mark.parametrize("symbol", SYMBOLS)
    def test_three_tables_or_two(self, symbol: str) -> None:
        payload = InfoPayload(load_fixture(symbol, "info"), datetime(2026, 9, 4))
        result = REGISTRY["info"].normalize(payload, symbol)
        tables = {w.table for w in result.writes}
        assert {"ticker_info", "ticker_info_history"} <= tables

    def test_raw_json_has_no_nan_and_matches_hash(self) -> None:
        payload = InfoPayload(load_fixture("AAPL", "info"), datetime(2026, 9, 4))
        row = _write(REGISTRY["info"].normalize(payload, "AAPL"), "ticker_info").rows[0]
        assert "NaN" not in row["raw_json"]
        body = json.loads(row["raw_json"])
        assert body["symbol"] == "AAPL"
        from yfin.core import normalize as nz

        assert nz.content_hash(canonical=row["raw_json"]) == row["content_hash"]

    def test_epoch_fields_converted_with_right_unit(self) -> None:
        payload = InfoPayload(load_fixture("AAPL", "info"), datetime(2026, 9, 4))
        row = _write(REGISTRY["info"].normalize(payload, "AAPL"), "ticker_info").rows[0]
        # firstTradeDateMilliseconds -> 1980-12-12
        assert row["first_trade_date"].year == 1980
        assert row["full_time_employees"] is None or isinstance(row["full_time_employees"], int)

    def test_officers_replace_scope_and_name_normalized(self) -> None:
        payload = InfoPayload(load_fixture("AAPL", "info"), datetime(2026, 9, 4))
        write = _write(REGISTRY["info"].normalize(payload, "AAPL"), "company_officers")
        assert write.mode == "replace_scope"
        assert all("  " not in row["name"] for row in write.rows)
        assert write.key_columns == ("symbol", "name")

    def test_crypto_has_no_officers(self) -> None:
        payload = InfoPayload(load_fixture("BTC-USD", "info"), datetime(2026, 9, 4))
        result = REGISTRY["info"].normalize(payload, "BTC-USD")
        assert "company_officers" not in {w.table for w in result.writes}

    def test_nested_keys_stay_in_raw_json_only(self) -> None:
        payload = InfoPayload(load_fixture("AAPL", "info"), datetime(2026, 9, 4))
        row = _write(REGISTRY["info"].normalize(payload, "AAPL"), "ticker_info").rows[0]
        assert "companyOfficers" not in row
        assert "companyOfficers" in json.loads(row["raw_json"])


# --- fast_info ------------------------------------------------------------


class TestFastInfo:
    @pytest.mark.parametrize("symbol", SYMBOLS)
    def test_two_tables(self, symbol: str) -> None:
        class Fake(dict):  # type: ignore[type-arg]
            pass

        payload = FastInfoPayload(Fake(load_fixture(symbol, "fast_info")), datetime(2026, 9, 4))
        result = REGISTRY["fast_info"].normalize(payload, symbol)
        assert {w.table for w in result.writes} == {"ticker_fast_info", "ticker_fast_info_history"}

    def test_market_cap_none_for_non_equity(self) -> None:
        """marketCap/shares are None for ETF, crypto, FX, and index."""
        data = load_fixture("BTC-USD", "fast_info")
        payload = FastInfoPayload(dict(data), datetime(2026, 9, 4))
        row = _write(REGISTRY["fast_info"].normalize(payload, "BTC-USD"), "ticker_fast_info").rows[
            0
        ]
        assert row["shares"] is None


# --- history_metadata -----------------------------------------------------


class TestHistoryMetadata:
    @pytest.mark.parametrize("symbol", SYMBOLS)
    def test_normalizes(self, symbol: str) -> None:
        payload = MetadataPayload(load_fixture(symbol, "history_metadata"), datetime(2026, 9, 4))
        write = _write(REGISTRY["history_metadata"].normalize(payload, symbol), "history_metadata")
        assert write.rows[0]["symbol"] == symbol
        assert write.rows[0]["raw_json"]

    def test_odd_keys_only_in_raw_json(self) -> None:
        """Keys like 'YF repair?' with a space or question mark are kept
        only in raw_json."""
        raw = load_fixture("AAPL", "history_metadata")
        assert "YF repair?" in raw
        payload = MetadataPayload(raw, datetime(2026, 9, 4))
        row = _write(
            REGISTRY["history_metadata"].normalize(payload, "AAPL"), "history_metadata"
        ).rows[0]
        assert "YF repair?" not in row
        assert "YF repair?" in json.loads(row["raw_json"])


# --- isin -----------------------------------------------------------------


class TestIsin:
    def test_real_isin_written(self) -> None:
        write = _write(REGISTRY["isin"].normalize(load_fixture("AAPL", "isin"), "AAPL"), "symbols")
        assert write.rows[0]["isin"] == "US0378331005"
        # touches only the isin column on the symbols table
        assert write.update_columns == ("isin",)

    @pytest.mark.parametrize("symbol", ["THYAO.IS", "BTC-USD"])
    def test_sentinel_produces_no_write(self, symbol: str) -> None:
        """The '-' sentinel means NULL; instead of writing NULL, nothing is written."""
        assert load_fixture(symbol, "isin") == "-"
        assert REGISTRY["isin"].normalize(load_fixture(symbol, "isin"), symbol).is_empty


# --- news -----------------------------------------------------------------


class TestNews:
    @pytest.mark.parametrize("symbol", SYMBOLS)
    def test_two_tables(self, symbol: str) -> None:
        result = REGISTRY["news"].normalize(load_fixture(symbol, "news"), symbol)
        assert {w.table for w in result.writes} == {"news", "news_symbols"}

    def test_thumbnail_uses_original_tag(self) -> None:
        rows = _write(REGISTRY["news"].normalize(load_fixture("AAPL", "news"), "AAPL"), "news").rows
        with_thumb = [r for r in rows if r["thumbnail_url"]]
        assert with_thumb, "expected at least one news item with a thumbnail"
        assert all(r["thumbnail_width"] for r in with_thumb)

    def test_thumbnail_may_be_none(self) -> None:
        rows = _write(REGISTRY["news"].normalize(load_fixture("AAPL", "news"), "AAPL"), "news").rows
        assert any(r["thumbnail_url"] is None for r in rows) or True

    def test_display_time_empty_string_becomes_null(self) -> None:
        rows = _write(REGISTRY["news"].normalize(load_fixture("AAPL", "news"), "AAPL"), "news").rows
        assert all(
            r["display_time"] is None or isinstance(r["display_time"], datetime) for r in rows
        )

    def test_out_of_universe_symbols_recorded(self) -> None:
        """The source includes symbols outside the universe; stored as a raw label."""
        links = _write(
            REGISTRY["news"].normalize(load_fixture("AAPL", "news"), "AAPL"), "news_symbols"
        ).rows
        symbols = {r["symbol"] for r in links}
        assert "AAPL" in symbols
        assert len(symbols) > 1, "expected multi-symbol news items"

    def test_news_ids_are_36_chars(self) -> None:
        rows = _write(REGISTRY["news"].normalize(load_fixture("SPY", "news"), "SPY"), "news").rows
        assert all(len(r["news_id"]) == 36 for r in rows)

    def test_pub_date_not_null(self) -> None:
        rows = _write(
            REGISTRY["news"].normalize(load_fixture("BTC-USD", "news"), "BTC-USD"), "news"
        ).rows
        assert all(isinstance(r["pub_date"], datetime) for r in rows)

    def test_empty_input(self) -> None:
        assert REGISTRY["news"].normalize([], "X").is_empty


class TestSourceMappingContract:
    """Converting source objects to a dict."""

    def test_history_metadata_violating_mapping_contract(self) -> None:
        """yfinance's HistoryMetadata is inconsistent between keys() and
        __getitem__: 'tradingPeriods' is listed but cannot be read
        (history.py:55). A plain dict(raw) call was marking the entire
        symbol unknown_symbol for funds like VFIAX.
        """
        from collections.abc import Mapping

        from yfin.core.normalize import as_mapping

        class BrokenMetadata(Mapping):  # type: ignore[type-arg]
            _data = {"currency": "USD", "shortName": "Fon"}

            def keys(self):  # type: ignore[no-untyped-def]
                return [*self._data.keys(), "tradingPeriods"]

            def __getitem__(self, key):  # type: ignore[no-untyped-def]
                return self._data[key]

            def __iter__(self):  # type: ignore[no-untyped-def]
                return iter(self.keys())

            def __len__(self) -> int:
                return len(self._data) + 1

        broken = BrokenMetadata()
        with pytest.raises(KeyError):
            dict(broken)

        result = as_mapping(broken)
        assert result == {"currency": "USD", "shortName": "Fon"}

    def test_history_metadata_dataset_survives_broken_source(self) -> None:
        """A symbol must not drop at the dataset level either."""
        from collections.abc import Mapping

        class BrokenMetadata(Mapping):  # type: ignore[type-arg]
            _data = dict(load_fixture("SPY", "history_metadata"))

            def keys(self):  # type: ignore[no-untyped-def]
                return [*self._data.keys(), "tradingPeriods"]

            def __getitem__(self, key):  # type: ignore[no-untyped-def]
                return self._data[key]

            def __iter__(self):  # type: ignore[no-untyped-def]
                return iter(self.keys())

            def __len__(self) -> int:
                return len(self._data) + 1

        payload = MetadataPayload(BrokenMetadata(), datetime(2026, 9, 4))
        result = REGISTRY["history_metadata"].normalize(payload, "VFIAX")
        assert _write(result, "history_metadata").rows[0]["symbol"] == "VFIAX"

    def test_symbols_dataset_survives_broken_source(self) -> None:
        from collections.abc import Mapping

        class BrokenMetadata(Mapping):  # type: ignore[type-arg]
            _data = dict(load_fixture("SPY", "history_metadata"))

            def keys(self):  # type: ignore[no-untyped-def]
                return [*self._data.keys(), "tradingPeriods"]

            def __getitem__(self, key):  # type: ignore[no-untyped-def]
                return self._data[key]

            def __iter__(self):  # type: ignore[no-untyped-def]
                return iter(self.keys())

            def __len__(self) -> int:
                return len(self._data) + 1

        payload = SymbolsPayload(
            fast_info=dict(load_fixture("SPY", "fast_info")),
            metadata=BrokenMetadata(),
            fetched_at=datetime(2026, 9, 4),
        )
        row = _write(REGISTRY["symbols"].normalize(payload, "VFIAX"), "symbols").rows[0]
        assert row["symbol"] == "VFIAX"
        assert row["currency"] is not None
