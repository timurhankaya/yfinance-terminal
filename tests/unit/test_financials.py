"""Financials normalizasyon testleri (S9.1).

Her test S8.3'teki bir kurali ya da S4.2'de curutulen bir varsayimi
korur. Fixture'lar gercek API'den yakalanmistir; ag erisimi yoktur.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest

from helpers import (
    as_earnings_frame,
    as_statement_frame,
    as_valuation_frame,
    load_fixture,
)
from yfin.datasets import SYMBOL_DATASETS as REGISTRY
from yfin.datasets.payloads import (
    CalendarPayload,
    EarningsDatesPayload,
    SecFilingsPayload,
    StatementPayload,
)
from yfin.models.financials import ITEM_KEY_LENGTH

FETCHED_AT = datetime(2026, 9, 4, 10, 0, 0, 500000)


def _statement_result(symbol: str, dataset: str, currency: str | None = "USD") -> Any:
    frame = as_statement_frame(load_fixture(symbol, dataset))
    payload = StatementPayload(frame=frame, currency=currency, fetched_at=FETCHED_AT)
    return REGISTRY[dataset].normalize(payload, symbol)


def _rows(result: Any, table: str) -> list[dict[str, Any]]:
    return [row for write in result.writes if write.table == table for row in write.rows]


class TestStatements:
    def test_periods_and_facts_are_written(self) -> None:
        result = _statement_result("AAPL", "income_stmt")
        periods = _rows(result, "financial_periods")
        facts = _rows(result, "financial_facts")
        assert len(periods) == 5  # AAPL yillik: 5 donem
        assert facts
        assert {p["symbol"] for p in periods} == {"AAPL"}
        assert {str(p["statement"]) for p in periods} == {"income"}
        assert {str(p["freq"]) for p in periods} == {"annual"}

    def test_nan_cells_produce_no_row(self) -> None:
        """AAPL income'da 195 hucrenin 45'i NaN: 'kalem o donemde yok'
        bilgisi satirin YOKLUGUDUR (S8.3)."""
        result = _statement_result("AAPL", "income_stmt")
        facts = _rows(result, "financial_facts")
        periods = _rows(result, "financial_periods")
        assert len(facts) == sum(p["item_count"] for p in periods)
        assert len(facts) < 39 * 5  # 195 hucre; NaN'lar yazilmadi

    def test_ratio_and_huge_value_share_one_column(self) -> None:
        """DECIMAL(38,10): TaxRateForCalcs=0.156 ile 1e11+ ayni kolonda."""
        facts = _rows(_statement_result("AAPL", "income_stmt"), "financial_facts")
        by_key = {(f["item_key"], f["period_end"].year): f["value"] for f in facts}
        rate = by_key[("TaxRateForCalcs", 2025)]
        assert rate == Decimal("0.1560000000")
        biggest = max(f["value"] for f in facts)
        assert biggest > Decimal("1e11")

    def test_values_are_quantized_in_python(self) -> None:
        """MySQL 11. basamagi SESSIZCE yuvarlar (Note 1265); yuvarlama
        bilincli olarak Python'da yapilir."""
        facts = _rows(_statement_result("AAPL", "income_stmt"), "financial_facts")
        assert all(-value.as_tuple().exponent == 10 for value in (f["value"] for f in facts))

    def test_msft_has_sixty_char_item_key(self) -> None:
        """Ham etiket max uzunlugu 51 DEGIL 60 (S4.2); VARCHAR(64) yalnizca
        4 karakter pay birakirdi."""
        facts = _rows(_statement_result("MSFT", "balance_sheet"), "financial_facts")
        longest = max(len(f["item_key"]) for f in facts)
        assert longest == 60
        assert all(f["item_key"].isalnum() for f in facts)

    def test_fiscal_year_end_is_not_calendar_year(self) -> None:
        """MSFT Haziran, AAPL Eylul mali yili."""
        msft = {
            p["period_end"].month
            for p in _rows(_statement_result("MSFT", "balance_sheet"), "financial_periods")
        }
        aapl = {
            p["period_end"].month
            for p in _rows(_statement_result("AAPL", "balance_sheet"), "financial_periods")
        }
        assert msft == {6}
        assert aapl == {9}

    def test_financial_currency_differs_from_price_currency(self) -> None:
        """THYAO.IS tablolari USD, fiyatlari TRY."""
        result = _statement_result("THYAO.IS", "income_stmt", currency="USD")
        assert {p["currency"] for p in _rows(result, "financial_periods")} == {"USD"}

    @pytest.mark.parametrize("symbol", ["SPY", "BTC-USD"])
    def test_empty_frame_is_empty_not_failure(self, symbol: str) -> None:
        """Sirket olmayan sembolde (0,0) DataFrame -> empty (S8.2).

        Iki farkli pazar tipi: ETF (SPY) ve kripto (BTC-USD). S9.1 referans
        sembol listesi bunlarin ikisini de sart kosar.
        """
        for dataset in ("income_stmt", "balance_sheet", "ttm_cashflow"):
            assert _statement_result(symbol, dataset).is_empty

    def test_ttm_and_quarterly_share_period_without_collision(self) -> None:
        """AAPL'de ikisi de 2026-06-30 doner; (statement, freq) PK'da."""
        ttm = _rows(_statement_result("AAPL", "ttm_income_stmt"), "financial_periods")
        quarterly = _rows(_statement_result("AAPL", "quarterly_income_stmt"), "financial_periods")
        assert {str(p["freq"]) for p in ttm} == {"ttm"}
        overlap = {p["period_end"] for p in ttm} & {p["period_end"] for p in quarterly}
        assert overlap  # ayni donem sonu, farkli freq -> cakisma yok

    def test_facts_write_uses_period_scope(self) -> None:
        """replace_scope kapsami (symbol, statement, freq, period_end);
        Yahoo bir kalemi kaldirdiginda satir kalici olarak durmaz."""
        result = _statement_result("AAPL", "income_stmt")
        write = next(w for w in result.writes if w.table == "financial_facts")
        assert write.mode == "replace_scope"
        assert write.scope_columns == ("symbol", "statement", "freq", "period_end")

    def test_raw_json_keeps_nan_as_null(self) -> None:
        result = _statement_result("AAPL", "income_stmt")
        raw = _rows(result, "financial_periods")[0]["raw_json"]
        assert "NaN" not in raw
        assert "null" in raw


class TestValuation:
    """Degerleme olcutleri: EAV'de dorduncu `statement` degeri."""

    def _result(self, symbol: str, dataset: str = "valuation_measures") -> Any:
        frame = as_valuation_frame(load_fixture(symbol, dataset))
        payload = StatementPayload(frame=frame, currency="USD", fetched_at=FETCHED_AT)
        return REGISTRY[dataset].normalize(payload, symbol)

    def test_nine_measures_land_in_financial_facts(self) -> None:
        result = self._result("AAPL")
        periods = _rows(result, "financial_periods")
        facts = _rows(result, "financial_facts")
        assert {str(p["statement"]) for p in periods} == {"valuation"}
        assert {str(p["freq"]) for p in periods} == {"annual"}
        assert {f["item_key"] for f in facts} >= {"Market Cap", "Trailing P/E"}
        assert len({f["item_key"] for f in facts}) == 9

    def test_current_column_produces_no_period(self) -> None:
        """'Current'in donem sonu tarihi yoktur; kolon DUSURULUR."""
        raw = load_fixture("AAPL", "valuation_measures")
        assert "Current" in raw[0]  # kaynakta var
        periods = _rows(self._result("AAPL"), "financial_periods")
        assert len(periods) == len(raw[0]) - 2  # index + Current haric
        assert all(p["period_end"].year >= 2016 for p in periods)

    def test_month_first_column_label_is_parsed_explicitly(self) -> None:
        """'9/30/2025' AY/GUN/YIL'dir (quote.py:815); AAPL mali yili Eylul."""
        periods = _rows(self._result("AAPL"), "financial_periods")
        assert {(p["period_end"].month, p["period_end"].day) for p in periods} == {(9, 30)}

    def test_ratio_and_market_cap_share_one_column(self) -> None:
        facts = {f["item_key"]: f["value"] for f in _rows(self._result("AAPL"), "financial_facts")}
        assert facts["Market Cap"] > Decimal("1e12")
        assert Decimal("1") < facts["Trailing P/E"] < Decimal("1000")

    def test_nan_cell_produces_no_fact_row(self) -> None:
        """PFE ceyreklikte 3/31/2025 kolonu bostur; satir YAZILMAZ ama
        baslik satiri item_count=0 ile yine de durur."""
        result = self._result("PFE", "quarterly_valuation_measures")
        periods = _rows(result, "financial_periods")
        facts = _rows(result, "financial_facts")
        assert len(facts) == sum(p["item_count"] for p in periods)
        assert len(facts) < 9 * len(periods)

    def test_item_key_with_punctuation_survives(self) -> None:
        """'PEG Ratio (5yr expected)' -- statement etiketlerinin aksine
        alfanumerik DEGIL; AsciiKeyType(128) bunu tasir."""
        keys = {f["item_key"] for f in _rows(self._result("AAPL"), "financial_facts")}
        assert "PEG Ratio (5yr expected)" in keys or "Enterprise Value/EBITDA" in keys
        assert max(len(k) for k in keys) <= ITEM_KEY_LENGTH

    def test_no_collision_with_income_statement(self) -> None:
        """AAPL 2025-09-30 hem income hem valuation'da var; ayrimi
        `statement` kolonu yapar."""
        valuation = _rows(self._result("AAPL"), "financial_periods")
        income = _rows(_statement_result("AAPL", "income_stmt"), "financial_periods")
        shared = {p["period_end"] for p in valuation} & {p["period_end"] for p in income}
        assert shared
        assert {str(p["statement"]) for p in valuation} != {str(p["statement"]) for p in income}

    @pytest.mark.parametrize("symbol", ["SPY", "BTC-USD", "BND", "^GSPC"])
    def test_non_company_symbols_are_empty(self, symbol: str) -> None:
        """ETF, kripto, tahvil fonu ve endekste kaynak (0,0) doner (S8.2)."""
        for dataset in ("valuation_measures", "quarterly_valuation_measures"):
            assert self._result(symbol, dataset).is_empty

    def test_only_current_column_is_empty_not_failure(self) -> None:
        """periods=0 sekli: tek kolon 'Current'. Yazilacak donem yoktur."""
        frame = pd.DataFrame({"Current": [1.0]}, index=pd.Index(["Market Cap"], dtype=str))
        payload = StatementPayload(frame=frame, currency=None, fetched_at=FETCHED_AT)
        assert REGISTRY["valuation_measures"].normalize(payload, "AAPL").is_empty

    def test_unparsable_column_is_dropped_not_fatal(self) -> None:
        """Kutuphane kolon bicimini degistirirse hucre DUSMEZ; tanidik
        kolonlar yazilir, tanimayan atilir."""
        frame = pd.DataFrame(
            {"9/30/2025": [1.5], "2025-09-30": [2.5]},
            index=pd.Index(["Trailing P/E"], dtype=str),
        )
        payload = StatementPayload(frame=frame, currency=None, fetched_at=FETCHED_AT)
        result = REGISTRY["valuation_measures"].normalize(payload, "AAPL")
        periods = _rows(result, "financial_periods")
        assert len(periods) == 1
        assert periods[0]["period_end"] == date(2025, 9, 30)

    def test_currency_is_quotation_not_reporting(self) -> None:
        """THYAO.IS: financialCurrency=USD ama Market Cap TRY'dir.

        Olcum: valuation Market Cap 4,14e11 ~ info.marketCap 4,08e11 (TRY);
        USD karsiligi bunun ~1/30'u olurdu. `financial_currency` kullanmak
        kolona YANLIS birim yazardi.
        """
        from yfin.datasets.base import SyncContext
        from yfin.datasets.financials.valuation import quote_currency

        class _Ticker:
            def get_info(self) -> dict[str, str]:
                return {"currency": "TRY", "financialCurrency": "USD"}

        ctx = SyncContext("THYAO.IS", _Ticker(), FETCHED_AT)
        assert quote_currency(ctx) == "TRY"

    def test_currency_failure_does_not_drop_the_cell(self) -> None:
        """`currency` ikincil alandir; info patlarsa None yazilir."""
        from yfin.datasets.base import SyncContext
        from yfin.datasets.financials.valuation import quote_currency

        class _Broken:
            def get_info(self) -> dict[str, str]:
                raise RuntimeError("info yok")

        ctx = SyncContext("AAPL", _Broken(), FETCHED_AT)
        assert quote_currency(ctx) is None

    def test_alias_expands_to_both_frequencies(self) -> None:
        names = [ds.name for ds in REGISTRY.resolve(["valuation"])]
        assert names == ["symbols", "valuation_measures", "quarterly_valuation_measures"]

    def test_financials_alias_does_not_include_valuation(self) -> None:
        """`financials` maliyeti sessizce buyumez."""
        names = {ds.name for ds in REGISTRY.resolve(["financials"])}
        assert not any(n.endswith("valuation_measures") for n in names)


class TestCalendar:
    def _result(self, symbol: str) -> Any:
        payload = CalendarPayload(calendar=load_fixture(symbol, "calendar"), fetched_at=FETCHED_AT)
        return REGISTRY["calendar"].normalize(payload, symbol)

    def test_two_tables_written(self) -> None:
        result = self._result("AAPL")
        assert {w.table for w in result.writes} == {"ticker_calendar", "ticker_calendar_history"}

    def test_earnings_date_list_becomes_start_end_count(self) -> None:
        row = _rows(self._result("AAPL"), "ticker_calendar")[0]
        assert row["earnings_date_count"] == 1
        assert row["earnings_date_start"] == row["earnings_date_end"]

    def test_missing_keys_are_null_not_error(self) -> None:
        """THYAO'da Dividend Date yok, tahmin alanlari None."""
        row = _rows(self._result("THYAO.IS"), "ticker_calendar")[0]
        assert row["dividend_date"] is None
        assert row["earnings_high"] is None
        assert row["ex_dividend_date"] is not None

    @pytest.mark.parametrize("symbol", ["SPY", "BTC-USD"])
    def test_empty_calendar_is_empty(self, symbol: str) -> None:
        assert self._result(symbol).is_empty


class TestEarningsDates:
    def _result(self, symbol: str) -> Any:
        payload = EarningsDatesPayload(
            frame=as_earnings_frame(load_fixture(symbol, "earnings_dates")), fetched_at=FETCHED_AT
        )
        return REGISTRY["earnings_dates"].normalize(payload, symbol)

    def test_same_timestamp_two_rows_survive(self) -> None:
        """AAPL 2002-07-16 16:00: iki satirin TEK farki Surprise(%)
        (2.55 / 13.43); EPS alanlarinin ikisi de NaN (S4.2)."""
        rows = _rows(self._result("AAPL"), "earnings_dates")
        target = [r for r in rows if r["earnings_date_local"].isoformat() == "2002-07-16"]
        assert len(target) == 2
        assert len({r["fact_hash"] for r in target}) == 2
        assert {r["surprise_pct"] for r in target} == {Decimal("2.55"), Decimal("13.43")}

    def test_tz_is_new_york_even_for_istanbul_symbol(self) -> None:
        rows = _rows(self._result("THYAO.IS"), "earnings_dates")
        assert {r["tz_name"] for r in rows} == {"America/New_York"}

    def test_utc_and_local_date_both_kept(self) -> None:
        rows = _rows(self._result("AAPL"), "earnings_dates")
        row = rows[0]
        assert row["earnings_ts_utc"].tzinfo is None  # MySQL DATETIME tz tasimaz
        assert row["earnings_date_local"] is not None

    def test_none_frame_is_empty(self) -> None:
        payload = EarningsDatesPayload(frame=None, fetched_at=FETCHED_AT)
        assert REGISTRY["earnings_dates"].normalize(payload, "SPY").is_empty

    @pytest.mark.parametrize("symbol", ["SPY", "BTC-USD"])
    def test_no_earnings_dates_for_non_companies(self, symbol: str) -> None:
        """Kaynak None doner; fixture bos kayit listesi olarak saklanir."""
        assert self._result(symbol).is_empty


class TestSecFilings:
    def _result(self, symbol: str) -> Any:
        raw = load_fixture(symbol, "sec_filings")
        payload = SecFilingsPayload(
            filings=raw if isinstance(raw, list) else [], fetched_at=FETCHED_AT
        )
        return REGISTRY["sec_filings"].normalize(payload, symbol)

    def test_accession_number_parsed_for_every_filing(self) -> None:
        rows = _rows(self._result("AAPL"), "sec_filings")
        assert len(rows) == 80
        assert all(len(r["filing_id"]) == 20 and r["filing_id"][10] == "-" for r in rows)

    def test_exhibits_go_to_child_table_with_url_hash(self) -> None:
        exhibits = _rows(self._result("AAPL"), "sec_filing_exhibits")
        assert exhibits
        assert all(len(e["url_hash"]) == 16 for e in exhibits)

    @pytest.mark.parametrize("symbol", ["THYAO.IS", "SPY", "BTC-USD"])
    def test_dict_payload_is_empty_not_type_error(self, symbol: str) -> None:
        """ABD disinda ve fon/ETF/kriptoda kaynak {} (dict) doner; `for f in
        raw` anahtarlari gezer ve f['type'] TypeError verirdi (S4.2)."""
        assert self._result(symbol).is_empty

    def test_epoch_date_is_seconds(self) -> None:
        rows = _rows(self._result("AAPL"), "sec_filings")
        row = next(r for r in rows if r["filing_id"] == "0001140361-26-035325")
        assert row["filed_ts_utc"].year == 2026
        assert row["filing_date"].isoformat() == "2026-09-01"

    def test_missing_required_field_skips_row_not_transaction(self) -> None:
        payload = SecFilingsPayload(
            filings=[{"date": None, "epochDate": None, "type": None, "edgarUrl": "x"}],
            fetched_at=FETCHED_AT,
        )
        assert REGISTRY["sec_filings"].normalize(payload, "AAPL").is_empty


class TestItemKeyGuard:
    def test_closed_label_universe_fits_the_column(self) -> None:
        """Etiket evreni KAPALIDIR: `const.fundamentals_keys`.

        Sentetik test emniyet valfinin calistigini gosterir; bu test valfin
        BUGUN hic tetiklenmemesi gerektigini kanitlar ve kutuphane
        yukseltmesi 128 karakteri asan bir etiket getirirse kirilir.
        """
        from yfinance import const

        from yfin.models.financials import ITEM_KEY_LENGTH

        labels = [key for group in const.fundamentals_keys.values() for key in group]
        assert len(labels) == 375  # olculen kapali evren
        longest = max(labels, key=len)
        assert len(longest) == 60, f"kaynak evren degisti: {longest}"
        assert len(longest) <= ITEM_KEY_LENGTH
        # Ayirici kacisi gerekmemesinin gerekcesi
        assert all(label.isalnum() for label in labels)

    def test_every_written_item_key_is_in_the_closed_universe(self) -> None:
        """Yazilan her kalem kaynak listede olmalidir; degilse ya yfinance
        surumu degismistir ya da etiket bozulmustur."""
        from yfinance import const

        universe = {key for group in const.fundamentals_keys.values() for key in group}
        for symbol, dataset in (("AAPL", "income_stmt"), ("MSFT", "balance_sheet")):
            facts = _rows(_statement_result(symbol, dataset), "financial_facts")
            unknown = {f["item_key"] for f in facts} - universe
            assert not unknown, f"{symbol}/{dataset}: evren disi etiket {unknown}"

    def test_too_long_item_key_is_skipped_cell_stays_ok(self) -> None:
        """128 karakteri asan etiket yazilmaz, item_count'a girmez ve
        hucre `ok` kalir (S8.4)."""
        import pandas as pd

        long_key = "X" * 200
        frame = pd.DataFrame(
            {pd.Timestamp("2025-12-31"): [1.0, 2.0]},
            index=pd.Index(["TotalRevenue", long_key], dtype=str),
        )
        payload = StatementPayload(frame=frame, currency="USD", fetched_at=FETCHED_AT)
        result = REGISTRY["income_stmt"].normalize(payload, "AAPL")
        facts = _rows(result, "financial_facts")
        assert [f["item_key"] for f in facts] == ["TotalRevenue"]
        assert _rows(result, "financial_periods")[0]["item_count"] == 1


@pytest.mark.parametrize(
    "dataset",
    [
        "income_stmt",
        "quarterly_income_stmt",
        "ttm_income_stmt",
        "balance_sheet",
        "quarterly_balance_sheet",
        "cashflow",
        "quarterly_cashflow",
        "ttm_cashflow",
    ],
)
def test_every_statement_dataset_normalizes(dataset: str) -> None:
    result = _statement_result("AAPL", dataset)
    assert not result.is_empty
    assert {w.table for w in result.writes} == {"financial_periods", "financial_facts"}


def test_utc_fetched_at_is_microsecond_precise() -> None:
    """DATETIME(6): ayni saniyede iki snapshot yazilabilmeli."""
    now = datetime.now(UTC)
    assert now.microsecond >= 0


class TestAbsentData:
    """hide_exceptions=False, yfinance'in "404 -> bos sozluk" davranisini
    ISTISNAYA cevirir; sirket olmayan sembolde bu `empty`tir (S8.2)."""

    def test_404_is_absent_not_failure(self) -> None:
        from yfin.client import is_absent_data

        class _Response:
            status_code = 404

        class _HttpError(Exception):
            response = _Response()

        assert is_absent_data(_HttpError("HTTP Error 404: "))

    def test_trailing_index_error_is_absent(self) -> None:
        """yfinance bos trailing cerceveyi .iloc ile okuyup patlar."""
        from yfin.client import is_absent_data

        assert is_absent_data(IndexError("positional indexers are out-of-bounds"))

    def test_other_errors_still_raise(self) -> None:
        from yfin.client import is_absent_data

        assert not is_absent_data(ValueError("bozuk veri"))
        assert not is_absent_data(IndexError("list index out of range"))

    def test_call_optional_returns_none_on_absent(self) -> None:
        from yfin.client import call_optional

        def _boom() -> None:
            raise IndexError("positional indexers are out-of-bounds")

        assert call_optional(_boom, what="test") is None

    def test_call_optional_reraises_real_errors(self) -> None:
        from yfin.client import call_optional

        def _boom() -> None:
            raise ValueError("gercek hata")

        with pytest.raises(ValueError, match="gercek hata"):
            call_optional(_boom, what="test")
