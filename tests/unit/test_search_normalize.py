"""SQ S7.1: search normalizasyonu -- GERCEK fixture'larla, agsiz."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from yfin.datasets.discovery.base import UNGATED_TABLES
from yfin.datasets.discovery.search import SearchDataset, SearchPayload

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "_discovery"
FETCHED_AT = datetime(2026, 9, 5, 12, 0, 0)
AS_OF = datetime(2026, 9, 5).date()


def _payload(fixture: str, term: str) -> SearchPayload:
    raw = json.loads((FIXTURES / f"{fixture}.json").read_text(encoding="utf-8"))
    return SearchPayload(
        query_term=term,
        as_of_date=AS_OF,
        fetched_at=FETCHED_AT,
        quotes=raw.get("quotes") or [],
        news=raw.get("news") or [],
        lists=raw.get("lists") or [],
        reports=raw.get("researchReports") or [],
    )


def _result(fixture: str, term: str) -> Any:
    return SearchDataset().normalize(_payload(fixture, term), term)


def _rows(result: Any, table: str) -> list[dict[str, Any]]:
    return [r for w in result.writes if w.table == table for r in w.rows]


@pytest.fixture(scope="module")
def aapl() -> Any:
    return _result("search_AAPL", "AAPL")


class TestCrunchbaseFilter:
    def test_symbolless_rows_are_dropped(self, aapl: Any) -> None:
        """REGRESYON (SQ K14).

        `include_cb=True` varsayilani Crunchbase ozel-sirket kayitlari
        donduruyor: `{index, name, permalink, isYahooFinance}` -- `symbol`
        YOK. `yfinance`in `.quotes` ozelligi bunlari suzuyor ama biz
        `.response` ham govdesini kullaniyoruz. Kor bir `q["symbol"]`
        KeyError verirdi; `.get()` ile yazilsaydi PK'ya NULL denenirdi.
        """
        payload = _payload("search_AAPL", "AAPL")
        symbolless = [q for q in payload.quotes if "symbol" not in q]
        assert symbolless, "fixture sembolsuz satir icermeli"
        assert len(_rows(aapl, "search_quotes")) == len(payload.quotes) - len(symbolless)

    def test_rank_stays_dense_after_filtering(self, aapl: Any) -> None:
        """Elenen satir `rank_index`i ATLATMAZ.

        Kaynak indeksi kullanilsaydi dizide delik olurdu ve "kacinci
        siradaydi" sorgusu yaniltirdi.
        """
        ranks = [r["rank_index"] for r in _rows(aapl, "search_quotes")]
        assert ranks == list(range(len(ranks)))


class TestQuoteProjection:
    def test_equity_carries_sector_and_industry(self, aapl: Any) -> None:
        row = next(r for r in _rows(aapl, "search_quotes") if r["symbol"] == "AAPL")
        assert row["sector"] == "Technology"
        assert row["industry"]

    def test_non_equity_leaves_sector_null(self) -> None:
        """SQ S4.1/2: sektor/endustri ailesi YALNIZ EQUITY'de gelir.
        Eksiklik hata degil, NULL."""
        rows = _rows(_result("search_BTC-USD", "BTC-USD"), "search_quotes")
        crypto = [r for r in rows if r["quote_type"] == "CRYPTOCURRENCY"]
        assert crypto
        assert all(r["sector"] is None for r in crypto)

    def test_raw_json_keeps_everything(self, aapl: Any) -> None:
        row = _rows(aapl, "search_quotes")[0]
        assert "isYahooFinance" in row["raw_json"]


class TestListsTwoShapes:
    def test_both_shapes_land_in_one_table(self) -> None:
        """SQ S4.1/6: `ALGO_WATCHLIST` (12 anahtar) ve
        `PREDEFINED_SCREENER` (9 anahtar). `list_type` AYIRICIDIR."""
        rows = _rows(_result("search_lists_two_shapes", "gold"), "search_lists")
        types = {r["list_type"] for r in rows}
        assert types == {"ALGO_WATCHLIST", "PREDEFINED_SCREENER"}

    def test_watchlist_shape_fields(self) -> None:
        rows = _rows(_result("search_lists_two_shapes", "gold"), "search_lists")
        row = next(r for r in rows if r["list_type"] == "ALGO_WATCHLIST")
        assert row["symbol_count"] is not None
        assert row["pf_id"] is not None
        # Diger seklin alanlari NULL
        assert row["total"] is None

    def test_screener_shape_uses_canonical_name_as_key(self) -> None:
        """`PREDEFINED_SCREENER` satirinda `slug` YOK; anahtar
        `canonicalName`dir ve bu ayni zamanda `yf.screen`e verilebilecek
        bir EKRAN ADIDIR (SQ S8.3 kesif sinyali)."""
        rows = _rows(_result("search_lists_two_shapes", "gold"), "search_lists")
        row = next(r for r in rows if r["list_type"] == "PREDEFINED_SCREENER")
        assert row["list_key"]
        assert row["total"] is not None
        assert row["symbol_count"] is None


class TestNews:
    def test_update_scope_is_narrow(self) -> None:
        """REGRESYON (SQ S8.5).

        Search haberi `Ticker.news` ile AYNI PK'yi paylasir ama govdesi
        DAR: 8 anahtara karsi 17. Kor upsert `summary`/`description`i
        NULL'lardi.
        """
        from yfin.datasets.discovery.search import NEWS_UPDATE
        from yfin.datasets.news import _NEWS_UPDATE

        for column in (
            "summary",
            "description",
            "canonical_url",
            "provider_url",
            "provider_source_id",
            "display_time",
            "thumbnail_url",
            "raw_json",
        ):
            assert column not in NEWS_UPDATE, column
        # Zengin yolun kapsami DEGISMEDI
        assert "summary" in _NEWS_UPDATE
        assert "raw_json" in _NEWS_UPDATE

    def test_related_tickers_become_links(self, aapl: Any) -> None:
        links = _rows(aapl, "news_symbols")
        assert links
        assert {"news_id", "symbol", "is_known"} == set(links[0])

    def test_thumbnail_original_resolution_is_used(self, aapl: Any) -> None:
        rows = [r for r in _rows(aapl, "news") if r["thumbnail_url"]]
        if rows:
            assert rows[0]["thumbnail_width"]

    def test_epoch_seconds_become_pub_date(self, aapl: Any) -> None:
        """`providerPublishTime` epoch SANIYEDIR; `Ticker.news`in
        `pubDate`i ISO metindir (SQ S4.2/6)."""
        assert all(hasattr(r["pub_date"], "year") for r in _rows(aapl, "news"))


class TestFreeTextQuery:
    def test_no_quotes_but_news_and_reports(self) -> None:
        """SQ S4.1/5 -- "Turkish Airlines": 0 quote, 5 haber, 3 rapor.

        `search_quotes` bos kalirken hucre `failed` DEGILDIR; durum TABLO
        BAZINDA turetilir (SQ S9.2).
        """
        result = _result("search_Turkish-Airlines", "Turkish Airlines")
        assert _rows(result, "search_quotes") == []
        assert _rows(result, "news")
        assert _rows(result, "research_reports")

    def test_empty_query_produces_nothing(self) -> None:
        result = _result("search_zzzqqxnope", "zzzqqxnope")
        assert all(not w.rows for w in result.writes)
        assert result.is_empty


class TestGateScope:
    def test_ungated_tables_are_declared(self) -> None:
        """REGRESYON (SQ S6.2.1).

        Dort tablo `query_term` TASIMAZ; kapili tarafa girselerdi
        `_first_row` yanlis satiri dondurur ve kapi yazimi KeyError
        verirdi. `research_reports` bu listeye DENETIMDE eklendi -- ilk
        tasarim yalniz ucunu sayiyordu.
        """
        assert {"symbols", "news", "news_symbols", "research_reports"} == UNGATED_TABLES

    def test_gated_tables_all_carry_gate_columns(self, aapl: Any) -> None:
        """Kapili her satir `query_term` + `as_of_date` + `fetched_at`
        tasimalidir; `_first_row` hangi write dolu olursa olsun dogru
        satiri bulsun diye."""
        for write in aapl.writes:
            if write.table in UNGATED_TABLES:
                continue
            for row in write.rows:
                assert {"query_term", "as_of_date", "fetched_at"} <= set(row), write.table

    def test_ungated_tables_lack_gate_columns(self, aapl: Any) -> None:
        """Listenin gerekcesini surer: bu tablolarda `query_term` YOK."""
        for write in aapl.writes:
            if write.table not in UNGATED_TABLES:
                continue
            for row in write.rows:
                assert "query_term" not in row, write.table

    def test_reports_are_written_before_hits(self, aapl: Any) -> None:
        """FK sirasi: `search_report_hits.report_id` -> `research_reports`.
        Ebeveyn ayni transaction'da ONCE yazilmali."""
        tables = [w.table for w in aapl.writes]
        assert tables.index("research_reports") < tables.index("search_report_hits")


class TestSymbolPromotion:
    def test_discovered_symbols_are_inactive(self, aapl: Any) -> None:
        rows = _rows(aapl, "symbols")
        assert rows
        assert all(r["is_active"] is False for r in rows)
        assert all(r["discovered_by"] == "search" for r in rows)

    def test_update_scope_excludes_activation_columns(self) -> None:
        from yfin.datasets.discovery.search import SYMBOL_UPDATE

        for column in ("is_active", "unknown_streak", "discovered_by", "discovered_at"):
            assert column not in SYMBOL_UPDATE

    def test_update_scope_is_narrower_than_screener(self) -> None:
        """SQ S5.12: Search kotasyonu `currency`/`timezone` TASIMAZ.

        Ortak bir liste kullanilsaydi bu yol her kosuda screener'in
        yazdigi o kolonlari NULL'lardi.
        """
        from yfin.datasets.discovery.search import SYMBOL_UPDATE
        from yfin.datasets.market.screener import SYMBOL_UPDATE as SCREENER_UPDATE

        assert set(SYMBOL_UPDATE) < set(SCREENER_UPDATE)
        assert "currency" not in SYMBOL_UPDATE


class TestReports:
    def test_epoch_ms_report_date(self, aapl: Any) -> None:
        """SQ S4.3: Search yolunda epoch MILISANIYE, domain yolunda ISO
        METIN. Ortak donusturucu biri icin sessiz NULL uretirdi."""
        rows = _rows(aapl, "research_reports")
        assert rows
        assert all(r["report_ts_utc"].year >= 2020 for r in rows)

    def test_search_only_columns_are_filled(self, aapl: Any) -> None:
        row = _rows(aapl, "research_reports")[0]
        assert row["author"]
        assert row["report_headline"]
        # Domain'e ozgu kolonlar bu yolda NULL kalir
        assert row["head_html"] is None
        assert row["report_type"] is None
