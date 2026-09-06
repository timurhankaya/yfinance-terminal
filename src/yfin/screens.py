"""Ekran tanimlarinin TEK kaynagi (SQ S6.5).

Iki kaynak ayrilir ve KARISTIRILMAZ:

- TANIMIN tek kaynagi bu dosyadir (`ScreenDef`).
- KOSU ANINDAKI ETKINLIGIN tek kaynagi `screens.is_enabled` kolonudur.
  `ScreenDef.is_enabled` yalnizca SEED degeridir; operator DB'de
  degistirdiginde bu dosya onu geri almaz.

19 predefined ekran KUTUPHANEDEN TURETILIR, elle yazilmaz: kutuphane bir
ekran ekledigi ya da kaldirdigi gun sessizce sapmayalim (SI S6.5 deseni,
`test_screens_match_library.py` surer).

Custom ekranlarda sorgu nesnesi ZORUNLUDUR ve kurulmasi dogrulanmasidir:
`EquityQuery`/`FundQuery`/`ETFQuery` gecersiz alan ya da deger icin AGA
CIKMADAN ValueError firlatir (SQ S4.1/14). Yani hatali bir tanim bu modul
import edilirken patlar, kosu ortasinda degil.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from yfinance import PREDEFINED_SCREENER_QUERIES
from yfinance.screener.query import EquityQuery, ETFQuery, FundQuery, QueryBase

ScreenKind = Literal["predefined", "custom"]
ScreenQuoteType = Literal["EQUITY", "MUTUALFUND", "ETF"]

# `sync_run_items.symbol` VARCHAR(32) ascii_bin'dir ve screener tarafinda
# kapsam etiketi olarak ekran anahtarini tasir (SQ S5.13). Olculen en uzun
# predefined ad `conservative_foreign_funds` = 26 karakter.
SCREEN_KEY_MAX_LENGTH = 32

# Sorgu SINIFI -> Yahoo'nun `quoteType` alani. `yf.screen` bu eslemeyi
# kendi icinde de yapiyor (screener.py, `isinstance` zinciri); burada
# TEKRARLANMASININ sebebi `screens` tablosuna yazilacak degerin kosudan
# ONCE bilinmesi gerekmesidir.
_QUOTE_TYPE_BY_QUERY_CLASS: dict[type[QueryBase], ScreenQuoteType] = {
    EquityQuery: "EQUITY",
    FundQuery: "MUTUALFUND",
    ETFQuery: "ETF",
}


@dataclass(frozen=True, slots=True)
class ScreenDef:
    """Tek bir ekranin tanimi.

    `query` predefined'da None'dir ve bu BILINCLIDIR: ad verildiginde
    `yf.screen` predefined GET yoluna gider ve ILK sayfa `title`,
    `description`, `rawCriteria`, `lastUpdated` gibi 12 ek alan getirir
    (SQ S4.1/13). Sorgu nesnesi tutulup POST yoluna dusulseydi bu metadata
    HIC alinamazdi.
    """

    key: str
    kind: ScreenKind
    quote_type: ScreenQuoteType
    title: str
    # SQ K15: `yf.screen`de `sortAsc` varsayilani None -> AZALAN. Sayfalar
    # arasi sira kararli olmazsa sayfalar ortusur ya da sembol atlanir, bu
    # yuzden her ekran kendi sirasini ACIKCA bildirir.
    sort_field: str
    sort_asc: bool = False
    description: str = ""
    # Yalnizca SEED degeri; kosu anindaki otorite `screens.is_enabled`.
    is_enabled: bool = True
    query: QueryBase | None = None

    def __post_init__(self) -> None:
        if len(self.key) > SCREEN_KEY_MAX_LENGTH or not self.key.isascii():
            raise ValueError(
                f"ekran anahtari en fazla {SCREEN_KEY_MAX_LENGTH} ASCII karakter: {self.key!r}"
            )
        if self.kind == "custom" and self.query is None:
            raise ValueError(f"custom ekran sorgu nesnesi tasimalidir: {self.key}")
        if self.kind == "predefined" and self.query is not None:
            raise ValueError(f"predefined ekran sorgu nesnesi TASIMAZ: {self.key}")


def _title_from_key(key: str) -> str:
    """`day_gainers` -> `Day Gainers`.

    Yalnizca SEED degeridir: predefined ekranlarda ilk GET sayfasi gercek
    basligi getirir ve `screens.title` tazelenir (SQ S7.3). Yine de bos
    birakilamaz -- kolon NOT NULL.
    """
    return key.replace("_", " ").title()


def _derive_predefined() -> tuple[ScreenDef, ...]:
    """`PREDEFINED_SCREENER_QUERIES`'ten 19 tanim uretir."""
    out: list[ScreenDef] = []
    for key, spec in PREDEFINED_SCREENER_QUERIES.items():
        query = spec["query"]
        quote_type = _QUOTE_TYPE_BY_QUERY_CLASS[type(query)]
        out.append(
            ScreenDef(
                key=key,
                kind="predefined",
                quote_type=quote_type,
                title=_title_from_key(key),
                sort_field=spec["sortField"],
                # Kutuphane bu alani hem 'DESC' hem 'desc' yaziyor
                # (`aggressive_small_caps` kucuk, `day_gainers` buyuk).
                sort_asc=spec["sortType"].lower() == "asc",
            )
        )
    return tuple(out)


PREDEFINED_SCREENS: tuple[ScreenDef, ...] = _derive_predefined()


# --- custom ekranlar -------------------------------------------------------
# Kurulmalari dogrulanmalaridir (SQ S4.1/14). `region` degerleri
# EQUITY_SCREENER_EQ_MAP['region'] ile sinirlidir; 'tr' olculdu ve
# gecerlidir (SQ S4.1/15, total=628).

CUSTOM_SCREENS: tuple[ScreenDef, ...] = (
    ScreenDef(
        key="tr_equity",
        kind="custom",
        quote_type="EQUITY",
        title="BIST Equities",
        description="Borsa Istanbul'da islem goren tum hisseler (region=tr).",
        # `ticker` + artan: sayfalar arasi KARARLI sira (SQ K15). Fiyat ya da
        # hacme gore siralansaydi iki sayfa arasinda sira degisip sembol
        # atlanabilirdi -- 628 satir uc sayfa demek (SQ S4.4).
        sort_field="ticker",
        sort_asc=True,
        query=EquityQuery(
            "and",
            [
                EquityQuery("eq", ["region", "tr"]),
                # `intradayprice > 0`: "hepsi" demenin yolu. Yahoo bos
                # operandli sorgu kabul etmiyor, tek kosullu bir AND de
                # `_validate_or_and_operand` tarafindan reddediliyor
                # (operand uzunlugu > 1 olmali).
                EquityQuery("gt", ["intradayprice", 0]),
            ],
        ),
    ),
)


ALL_SCREENS: tuple[ScreenDef, ...] = (*PREDEFINED_SCREENS, *CUSTOM_SCREENS)

_BY_KEY: dict[str, ScreenDef] = {s.key: s for s in ALL_SCREENS}

if len(_BY_KEY) != len(ALL_SCREENS):  # pragma: no cover - savunma
    raise ValueError("ekran anahtarlari tekil olmalidir")


def screen_by_key(key: str) -> ScreenDef:
    """Bilinmeyen ad KeyError firlatir; sessizce None DONMEZ.

    `screens` tablosunda olup bu dosyada olmayan bir anahtar, seed'den sonra
    elle eklenmis demektir ve `screener` dataset'i onu kosturamaz -- sorgu
    govdesini nereden alacagini bilemez.
    """
    return _BY_KEY[key]
