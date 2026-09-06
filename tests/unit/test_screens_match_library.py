"""SQ S6.5: predefined ekran kumesi KUTUPHANEDEN turetilir, elle yazilmaz.

Bu testin varlik sebebi SI S6.5'in `domain_key` kumesi icin koydugu kuralla
aynidir: kutuphane bir ekran ekledigi ya da kaldirdigi gun sessizce
sapmayalim. Elle yazilmis bir liste bunu ancak birileri fark edince
gosterirdi.
"""

from __future__ import annotations

from yfinance import PREDEFINED_SCREENER_QUERIES

from yfin.screens import PREDEFINED_SCREENS, ScreenDef


def test_predefined_keys_match_library() -> None:
    assert {s.key for s in PREDEFINED_SCREENS} == set(PREDEFINED_SCREENER_QUERIES)


def test_predefined_sort_matches_library() -> None:
    """`sort_field` / `sort_asc` kutuphanedeki tanimla BIREBIR ayni.

    SQ K15: sira acikca verilir. Kutuphanenin sirasindan sapilsaydi bizim
    yazdigimiz kadro Yahoo'nun kendi ekraninkinden farkli olurdu ve
    `screen_members.rank` baska bir seyi olcerdi.
    """
    for screen in PREDEFINED_SCREENS:
        spec = PREDEFINED_SCREENER_QUERIES[screen.key]
        assert screen.sort_field == spec["sortField"]
        assert screen.sort_asc == (spec["sortType"].lower() == "asc")


def test_predefined_quote_type_matches_query_class() -> None:
    """quote_type, kutuphanedeki sorgu SINIFINDAN turetilir."""
    expected = {
        "EquityQuery": "EQUITY",
        "FundQuery": "MUTUALFUND",
        "ETFQuery": "ETF",
    }
    for screen in PREDEFINED_SCREENS:
        cls = type(PREDEFINED_SCREENER_QUERIES[screen.key]["query"]).__name__
        assert screen.quote_type == expected[cls]


def test_predefined_carries_no_query_object() -> None:
    """Predefined'da `query` None'dir: ad yeterlidir ve ILK sayfa GET yolundan
    metadata getirir (SQ S4.1/13). Sorgu nesnesi tutulsaydi POST yoluna
    dusulur ve `title`/`rawCriteria` hic alinamazdi."""
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
    raise AssertionError("ScreenDef frozen olmali")
