"""SQ S9.4: ekran tanimlari AGSIZ dogrulanir.

`EquityQuery`/`FundQuery`/`ETFQuery` gecersiz alan ya da deger icin AGA
CIKMADAN ValueError firlatir (SQ S4.1/14). Bu yuzden hatali bir tanim
kosu ortasinda degil, modul yuklenirken ortaya cikar -- ve bu test onu
CI'da yakalar, uretimde degil.
"""

from __future__ import annotations

import pytest

from yfin.screens import (
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
    """SQ S5.8: sinir `sync_run_items.symbol` = VARCHAR(32) ascii_bin'den gelir.

    Olculen en uzun predefined ad `conservative_foreign_funds` = 26.
    """
    for screen in ALL_SCREENS:
        assert len(screen.key) <= SCREEN_KEY_MAX_LENGTH, screen.key
        assert screen.key.isascii(), screen.key


def test_custom_screens_carry_a_query() -> None:
    """Custom'da sorgu nesnesi ZORUNLU: ad yoktur, POST govdesi ondan uretilir."""
    for screen in CUSTOM_SCREENS:
        assert screen.kind == "custom"
        assert screen.query is not None


def test_custom_queries_are_constructible() -> None:
    """Kurulabilmis olmalari zaten dogrulandiklari anlamina gelir (istemci
    tarafi dogrulama, SQ S4.1/14). Burada `to_dict()` ile POST govdesinin de
    uretilebildigi surulur."""
    for screen in CUSTOM_SCREENS:
        assert screen.query is not None
        body = screen.query.to_dict()
        assert "operator" in body
        assert "operands" in body


def test_every_screen_declares_sort() -> None:
    """SQ K15: `sortAsc` varsayilani azalan; sayfalar arasi sira kararli
    olmazsa sayfalar ORTUSUR ya da sembol ATLANIR."""
    for screen in ALL_SCREENS:
        assert screen.sort_field
        assert isinstance(screen.sort_asc, bool)


def test_quote_type_is_known() -> None:
    for screen in ALL_SCREENS:
        assert screen.quote_type in {"EQUITY", "MUTUALFUND", "ETF"}


def test_titles_are_present() -> None:
    """`screens.title` NOT NULL yazilir; predefined'da ILK GET sayfasindan
    tazelenir ama seed degeri bos olamaz (SQ S5.8)."""
    for screen in ALL_SCREENS:
        assert screen.title.strip()


def test_screen_by_key_roundtrip() -> None:
    for screen in ALL_SCREENS:
        assert screen_by_key(screen.key) is screen


def test_screen_by_key_rejects_unknown() -> None:
    with pytest.raises(KeyError):
        screen_by_key("nosuchscreen")


def test_tr_equity_custom_screen_exists() -> None:
    """SQ S4.1/15: `region=tr` olculdu, total=628. BIST kesfinin tek custom
    girisidir."""
    screen = screen_by_key("tr_equity")
    assert screen.kind == "custom"
    assert screen.quote_type == "EQUITY"
    assert screen.sort_asc is True
