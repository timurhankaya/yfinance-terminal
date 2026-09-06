"""Canli Yahoo dogrulamalari -- Search / Lookup / Screener (SQ S10.4).

CI'da KAPALI: `-m live` gerekir.

Bu dosyadaki testlerin cogu bir SAYIYI degil bir ILISKIYI surer. Sayilar
gun icinde oynuyor (`day_gainers` iki olcum arasinda 122 -> 117 gitti);
kilitlenen sey tasarimin dayandigi DAVRANIS.
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
from yfin.screens import screen_by_key

pytestmark = pytest.mark.live

NOW = datetime.now(UTC).replace(tzinfo=None)


def _ctx(term: str) -> SyncContext:
    return SyncContext(symbol=term, ticker=None, fetched_at=NOW)


def _mctx(variant: str) -> MarketContext:
    return MarketContext(
        fetched_at=NOW, start=NOW.date(), end=NOW.date()
    ).for_variant(variant)


# --- Lookup: K6'nin iki yuzu ----------------------------------------------
#
# IKISI BIRLIKTE ZORUNLUDUR. Tasarimin ilk hali YALNIZ dar terimle
# olculup genellenmisti ve yanlis bir degismezi kilitlemisti; tek basina
# dar test ayni hatayi yeniden uretirdi.


def test_narrow_term_all_equals_typed_union() -> None:
    """DAR terim: `all` tipli birlesimin TAMAMINI verir, fark IKI YONDE 0."""
    term = "AAPL"
    block = _fetch_type(term, ALL_TYPE, 1000)
    all_symbols = {d["symbol"] for d in block["documents"] if "symbol" in d}
    assert block["lookupTotals"]["all"] <= 500, "AAPL dar terim olmali"

    union: set[str] = set()
    for lookup_type in TYPED_LOOKUPS:
        typed = _fetch_type(term, lookup_type, 1000)
        union |= {d["symbol"] for d in typed.get("documents") or [] if "symbol" in d}

    assert all_symbols - union == set()
    assert union - all_symbols == set()


def test_broad_term_all_is_truncated_and_typed_union_is_wider() -> None:
    """GENIS terim: `all` ~1.000'de KIRPILIR ve tipli birlesim KAT KAT genis.

    Olculen (2026-09-05): GOLD -> `lookupTotals.all` 7.273, `all` 995 belge,
    tipli birlesim 3.313; fark IKI YONLU (354 / 2.671).

    Bu test kirmizi olursa K6'nin adaptif dali gereksizlesmis demektir --
    ve o zaman KALDIRILMALIDIR, sessizce tutulmamalidir.
    """
    term = "GOLD"
    block = _fetch_type(term, ALL_TYPE, 1000)
    all_symbols = {d["symbol"] for d in block["documents"] if "symbol" in d}
    reported = block["lookupTotals"]["all"]

    assert reported > 1000, "GOLD genis terim olmali"
    assert len(all_symbols) < reported, "`all` kirpilmali"

    union: set[str] = set()
    for lookup_type in TYPED_LOOKUPS:
        typed = _fetch_type(term, lookup_type, 1000)
        union |= {d["symbol"] for d in typed.get("documents") or [] if "symbol" in d}

    assert len(union) > len(all_symbols) * 2, (len(union), len(all_symbols))


def test_lookup_dataset_writes_totals_for_nine_types() -> None:
    """`privateCompany` `LOOKUP_TYPES` sabitinde YOK; yanittan okunur."""
    payload = LookupDataset().fetch(_ctx("BTC"))
    assert "privateCompany" in payload.totals
    assert len(payload.totals) >= 9


# --- Search: varsayilan olmayan bayraklar ----------------------------------


def test_research_requires_an_explicit_flag() -> None:
    """`include_research` VARSAYILANI FALSE (search.py:32-34).

    Acikca verilmezse `research_reports` ve `search_report_hits` HIC satir
    almaz -- ve S9.6 eksiksizlik kaniti bunu YAKALAMAZ, cunku "kaynak bos
    dondu" ile "istemedik" ayni gorunur.
    """
    default = yf.Search("AAPL").response
    explicit = yf.Search("AAPL", include_research=True).response
    assert not default.get("researchReports")
    assert explicit.get("researchReports")


def test_search_quotes_carry_symbolless_rows() -> None:
    """SQ K14: `include_cb=True` varsayilani Crunchbase kayitlari getiriyor.

    Kor bir `q["symbol"]` KeyError verirdi. Serbest terimde daha gorunur.
    """
    raw = yf.Search("gold", max_results=10).response
    quotes = raw.get("quotes") or []
    assert quotes
    assert any("symbol" not in q for q in quotes), "sembolsuz satir bekleniyordu"


def test_search_dataset_drops_symbolless_rows() -> None:
    payload = SearchDataset().fetch(_ctx("AAPL"))
    result = SearchDataset().normalize(payload, "AAPL")
    rows = [r for w in result.writes if w.table == "search_quotes" for r in w.rows]
    assert rows
    assert all(r["symbol"] for r in rows)
    assert [r["rank_index"] for r in rows] == list(range(len(rows)))


# --- Screener: sayfalama ve sira ------------------------------------------


def test_paging_needs_size_not_count() -> None:
    """SQ K12: `offset` verildiginde `count` SESSIZCE yok sayilir."""
    with_count = yf.screen("top_mutual_funds", offset=250, count=250)
    with_size = yf.screen("top_mutual_funds", offset=250, size=250)
    assert len(with_count["quotes"]) < len(with_size["quotes"])
    assert len(with_size["quotes"]) == 250


def test_offset_beyond_total_returns_empty_without_error() -> None:
    """Durma kosulunun ucuncu dali: hata YOK, 0 satir."""
    page = yf.screen("day_gainers", offset=9000, size=25)
    assert page["quotes"] == []


def test_custom_screen_first_page_carries_no_metadata() -> None:
    """Custom ekranin ILK sayfasi da POST'tur: 5 anahtar, `title` YOK."""
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
    """`tr_equity` total=628 olculdu; dort sayfa hepsini almali."""
    from yfin.config import get_settings

    cfg = get_settings().model_copy(update={"yf_screen_max_pages": 4, "yf_screen_size": 250})
    import yfin.config as config_mod

    original = config_mod.get_settings
    config_mod.get_settings = lambda: cfg  # type: ignore[assignment]
    try:
        payload = ScreenerDataset().fetch(_mctx("tr_equity"))
    finally:
        config_mod.get_settings = original  # type: ignore[assignment]

    assert payload.total > 500
    assert len(payload.quotes) == payload.total
    symbols = [q["symbol"] for q in payload.quotes]
    assert symbols == sorted(symbols), "sortAsc=True kararli artan sira vermeli"
    assert len(set(symbols)) == len(symbols), "sayfalar ORTUSMEMELI"
