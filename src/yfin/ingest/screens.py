"""Single source of screen definitions.

This file owns the DEFINITION; the `screens.is_enabled` column owns runtime
enablement, and `ScreenDef.is_enabled` is only a seed. Predefined screens are
derived from the library; custom ones fail at import time on a bad query."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from yfinance import PREDEFINED_SCREENER_QUERIES
from yfinance.screener.query import EquityQuery, ETFQuery, FundQuery, QueryBase

from yfin.models.discovery import SCREEN_KEY_LENGTH

ScreenKind = Literal["predefined", "custom"]
ScreenQuoteType = Literal["EQUITY", "MUTUALFUND", "ETF"]

# Derived from the schema column length rather than duplicated as a literal:
# if they diverge, validation checks itself instead of the schema.
SCREEN_KEY_MAX_LENGTH = SCREEN_KEY_LENGTH

# Query class -> Yahoo's `quoteType` field. `yf.screen` does this same
# mapping internally (screener.py, an isinstance chain); it's duplicated
# here because the value to write to the `screens` table must be known
# before the run.
_QUOTE_TYPE_BY_QUERY_CLASS: dict[type[QueryBase], ScreenQuoteType] = {
    EquityQuery: "EQUITY",
    FundQuery: "MUTUALFUND",
    ETFQuery: "ETF",
}


@dataclass(frozen=True, slots=True)
class ScreenDef:
    """Definition of a single screen.

    `query` is None for predefined screens: with just a name `yf.screen` takes
    the GET path, whose first page returns metadata the POST path never does."""

    key: str
    kind: ScreenKind
    quote_type: ScreenQuoteType
    title: str
    # `yf.screen`'s default for `sortAsc` is None -> descending. If order
    # isn't stable across pages, pages overlap or a symbol is skipped, so
    # each screen declares its order explicitly.
    sort_field: str
    sort_asc: bool = False
    description: str = ""
    # Seed value only; runtime authority is `screens.is_enabled`.
    is_enabled: bool = True
    query: QueryBase | None = None

    def __post_init__(self) -> None:
        if len(self.key) > SCREEN_KEY_MAX_LENGTH or not self.key.isascii():
            raise ValueError(
                f"screen key must be at most {SCREEN_KEY_MAX_LENGTH} ASCII characters: "
                f"{self.key!r}"
            )
        if self.kind == "custom" and self.query is None:
            raise ValueError(f"custom screen must carry a query object: {self.key}")
        if self.kind == "predefined" and self.query is not None:
            raise ValueError(f"predefined screen must NOT carry a query object: {self.key}")


def _title_from_key(key: str) -> str:
    """`day_gainers` -> `Day Gainers`.

    Seed value only: the first GET page refreshes `screens.title` with the
    real one. The column is NOT NULL, so it cannot be blank."""
    return key.replace("_", " ").title()


def _derive_predefined() -> tuple[ScreenDef, ...]:
    """Builds the 19 definitions from `PREDEFINED_SCREENER_QUERIES`."""
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
                # The library writes this field as both 'DESC' and 'desc'
                # (`aggressive_small_caps` lowercase, `day_gainers` upper).
                sort_asc=spec["sortType"].lower() == "asc",
            )
        )
    return tuple(out)


PREDEFINED_SCREENS: tuple[ScreenDef, ...] = _derive_predefined()


# --- custom screens ---------------------------------------------------------
# Building them is their own validation. `region` values are limited to
# EQUITY_SCREENER_EQ_MAP['region'].

CUSTOM_SCREENS: tuple[ScreenDef, ...] = (
    ScreenDef(
        key="tr_equity",
        kind="custom",
        quote_type="EQUITY",
        title="BIST Equities",
        description="All shares traded on Borsa Istanbul (region=tr).",
        # `ticker` + ascending: stable order across pages. Sorting by price
        # or volume could reorder between two pages and skip a symbol.
        sort_field="ticker",
        sort_asc=True,
        query=EquityQuery(
            "and",
            [
                EquityQuery("eq", ["region", "tr"]),
                # `intradayprice > 0` is how you say "all of them": Yahoo
                # rejects a query with no operands, and a single-condition
                # AND is also rejected by `_validate_or_and_operand`
                # (operand length must be > 1).
                EquityQuery("gt", ["intradayprice", 0]),
            ],
        ),
    ),
)


ALL_SCREENS: tuple[ScreenDef, ...] = (*PREDEFINED_SCREENS, *CUSTOM_SCREENS)

_BY_KEY: dict[str, ScreenDef] = {s.key: s for s in ALL_SCREENS}

if len(_BY_KEY) != len(ALL_SCREENS):  # pragma: no cover - defensive
    raise ValueError("screen keys must be unique")


def screen_by_key(key: str) -> ScreenDef:
    """Raises KeyError for an unknown name; never silently returns None.

    A key in the `screens` table but not here was added by hand after seeding,
    and the `screener` dataset has no query body to run it with."""
    return _BY_KEY[key]
