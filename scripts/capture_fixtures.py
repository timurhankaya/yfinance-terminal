"""Fetches data once from the real API and saves it as a fixture.

Usage: capture_fixtures.py [SYMBOL ...] | --bars [SYMBOL ...] | --domain |
       --discovery | --screen | _market"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yfinance as yf

from yfin.core import normalize as nz
from yfin.core.config import get_settings
from yfin.datasets.funds import _collect

# Each symbol proves one edge case on its own (fiscal year not pinned to the
# calendar, duplicate insider rows, a bond fund, an index with every dataset
# empty); removing one silently drops coverage for that case.
REFERENCE_SYMBOLS = (
    "AAPL",
    "MSFT",
    "THYAO.IS",
    "SPY",
    "BTC-USD",
    "PFE",
    "XOM",
    "NVDA",
    "WMT",
    "KO",
    "BND",
    "^GSPC",
)

# 16 datasets served by 7 requests through one `Ticker`.
# `sustainability` is deliberately absent: it returns 404 on all 19 symbols.
ANALYSIS_GETTERS = (
    "get_recommendations",
    "get_upgrades_downgrades",
    "get_analyst_price_targets",
    "get_earnings_estimate",
    "get_revenue_estimate",
    "get_eps_trend",
    "get_eps_revisions",
    "get_earnings_history",
    "get_growth_estimates",
)
HOLDERS_GETTERS = (
    "get_major_holders",
    "get_institutional_holders",
    "get_mutualfund_holders",
    "get_insider_purchases",
    "get_insider_transactions",
    "get_insider_roster_holders",
)

# (dataset adi, statement metodu, freq)
STATEMENT_SPECS = (
    ("income_stmt", "get_income_stmt", "yearly"),
    ("quarterly_income_stmt", "get_income_stmt", "quarterly"),
    ("ttm_income_stmt", "get_income_stmt", "trailing"),
    ("balance_sheet", "get_balance_sheet", "yearly"),
    ("quarterly_balance_sheet", "get_balance_sheet", "quarterly"),
    ("cashflow", "get_cashflow", "yearly"),
    ("quarterly_cashflow", "get_cashflow", "quarterly"),
    ("ttm_cashflow", "get_cashflow", "trailing"),
)

# (dataset name, freq). 'trailing' and 'monthly' don't exist here; see the
# note at the end of `datasets/financials/valuation.py`.
VALUATION_SPECS = (
    ("valuation_measures", "yearly"),
    ("quarterly_valuation_measures", "quarterly"),
)

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

# price_bars fixture symbols. Each proves one edge case on its own: extended
# hours, hasPrePost=False yet extended bars returned, degenerate pre/post
# columns, 24/7 trading, and a bar whose local_date is not the session day.
BAR_FIXTURE_SYMBOLS = ("AAPL", "SHEL.L", "VWCE.DE", "THYAO.IS", "BTC-USD", "GC=F")


def _frame_records(obj: Any) -> Any:
    import pandas as pd

    if isinstance(obj, pd.DataFrame):
        # Leaving the index to to_json flattens tz info to UTC, losing the
        # point of the local-session-date test; so it's written separately.
        records = []
        for idx, row in zip(obj.index, obj.to_dict("records"), strict=True):
            entry: dict[str, Any] = {"index": str(idx)}
            entry.update({k: (None if nz.is_missing(v) else v) for k, v in row.items()})
            records.append(entry)
        return records
    if isinstance(obj, pd.Series):
        return [
            {"index": str(idx), "value": None if nz.is_missing(val) else val}
            for idx, val in obj.items()
        ]
    return obj


def _capture_optional(ticker: Any, getter: str) -> Any:
    """404 and a parse error both mean no data; fixture stores `null`.

    This capture script must not crash on one exotic symbol: ^GSPC returns
    404 for all 16 datasets, and that's expected.
    """
    try:
        return _frame_records(getattr(ticker, getter)())
    except Exception as exc:  # noqa: BLE001 - capture script, not a code path
        print(f"  {getter}: no data ({type(exc).__name__})")
        return None


def _capture_funds(ticker: Any) -> Any:
    """A non-fund symbol raises a raw `KeyError('topHoldings')`."""
    try:
        # `_read` is required: `quote_type` is a method, not a @property,
        # in yfinance 1.7.0.
        collected = _collect(ticker.get_funds_data())
        return {
            key: (_frame_records(value) if hasattr(value, "columns") else value)
            for key, value in collected.items()
        }
    except Exception as exc:  # noqa: BLE001
        print(f"  funds_data: not a fund ({type(exc).__name__})")
        return None


def capture(symbol: str) -> dict[str, Any]:
    ticker = yf.Ticker(symbol)
    out: dict[str, Any] = {}

    out["isin"] = ticker.get_isin()
    out["info"] = dict(ticker.get_info())
    fast = ticker.get_fast_info()
    out["fast_info"] = {k: fast[k] for k in fast}
    out["history_metadata"] = dict(ticker.get_history_metadata())
    # Last 400 sessions, to keep fixture size reasonable
    out["history"] = _frame_records(
        ticker.history(period="2y", interval="1d", auto_adjust=False, actions=True)
    )
    out["dividends"] = _frame_records(ticker.get_dividends(period="max"))
    out["splits"] = _frame_records(ticker.get_splits(period="max"))
    out["capital_gains"] = _frame_records(ticker.get_capital_gains(period="max"))
    shares = ticker.get_shares_full(start="1970-01-01")
    out["shares_full"] = None if shares is None else _frame_records(shares)
    out["news"] = yf.Ticker(symbol).get_news(
        count=get_settings().yf_news_count, tab=get_settings().yf_news_tab
    )

    # --- financials ---
    for name, method, freq in STATEMENT_SPECS:
        frame = getattr(ticker, method)(pretty=False, freq=freq)
        out[name] = _frame_records(frame)
    # Column labels ('Current', 'M/D/YYYY') are kept raw in the fixture:
    # converting to a date is the dataset's job, and the test must verify it.
    for name, freq in VALUATION_SPECS:
        out[name] = _frame_records(ticker.get_valuation_measures(freq=freq, periods=None))
    out["calendar"] = dict(ticker.get_calendar() or {})
    # Pagination needs a fresh Ticker (base.py:637's cache ignores offset)
    earnings = yf.Ticker(symbol).get_earnings_dates(limit=100, offset=0)
    # The ISO string only preserves the offset; the tz name is stored
    # separately, or the "America/New_York even for THYAO" finding would
    # be lost in the fixture
    out["earnings_dates"] = {
        "tz": str(getattr(earnings, "index", None).tz) if earnings is not None else None,
        "records": _frame_records(earnings),
    }
    sec = ticker.get_sec_filings()
    out["sec_filings"] = sec if isinstance(sec, list) else []

    # Analyst + ownership: reuses the same Ticker; a fresh Ticker would
    # raise the cost from 7 requests to 16.
    for getter in (*ANALYSIS_GETTERS, *HOLDERS_GETTERS):
        name = getter.removeprefix("get_")
        out[name] = _capture_optional(ticker, getter)
    out["funds_data"] = _capture_funds(ticker)
    return out


def capture_bars(symbol: str) -> dict[str, Any]:
    """price_bars fixture: frame + tradingPeriods.

    5m rather than 1m: it tests the same is_extended logic and, unlike 1m,
    can still be recaptured after 30 days."""
    ticker = yf.Ticker(symbol)
    frame = ticker.history(
        period="5d",
        interval="5m",
        auto_adjust=False,
        actions=True,
        prepost=True,
        repair=False,
    )
    metadata = ticker.get_history_metadata()
    periods = metadata.get("tradingPeriods")
    return {
        "bars_5m": {
            "frame": _frame_records(frame),
            "trading_periods": _frame_records(periods),
            # Not part of any rule; kept to document that SHEL.L/VWCE.DE
            # return extra bars despite reporting False here.
            "has_pre_post_market_data": metadata.get("hasPrePostMarketData"),
            "exchange_timezone": metadata.get("exchangeTimezoneName"),
        }
    }


def capture_weekly(symbol: str) -> dict[str, Any]:
    """1wk/1mo fixture: is_extended must always be 0."""
    ticker = yf.Ticker(symbol)
    out: dict[str, Any] = {}
    for name, interval in (("bars_1wk", "1wk"), ("bars_1mo", "1mo")):
        frame = ticker.history(
            period="2y", interval=interval, auto_adjust=False, actions=True, repair=False
        )
        out[name] = {
            "frame": _frame_records(frame),
            "trading_periods": None,
            "has_pre_post_market_data": None,
            "exchange_timezone": str(getattr(frame.index, "tz", None)),
        }
    return out


def capture_market() -> dict[str, Any]:
    """Market fixtures: status is expected to be None in the 7 non-US regions."""
    from yfinance import Calendars, Market

    out: dict[str, Any] = {}
    market = Market("US")
    out["market_status"] = dict(market.status or {})
    out["market_summary"] = {k: dict(v) for k, v in (market.summary or {}).items()}
    calendars = Calendars()
    out["earnings_calendar"] = _frame_records(
        calendars.get_earnings_calendar(limit=100, offset=0, filter_most_active=False)
    )
    out["economic_calendar"] = _frame_records(calendars.get_economic_events_calendar(limit=100))
    out["ipo_calendar"] = _frame_records(calendars.get_ipo_info_calendar(limit=100))
    out["splits_calendar"] = _frame_records(calendars.get_splits_calendar(limit=100))
    return out


# --- domain (sector / industry) fixtures -----------------------------
#
# Each reference key proves one edge case on its own:
DOMAIN_SECTOR_FIXTURES: tuple[tuple[str, str], ...] = (
    # Full response: 12 industries, 10 ETFs + 10 funds, 4 reports,
    # performance + benchmark
    ("technology", "US"),
    # 6/6 industry keys differ from the library constant -- core proof of
    # the key-source rule
    ("utilities", "US"),
    # Fund symbol 0P0001WO1I (Morningstar id, not a ticker) -> is_known=0
    ("healthcare", "US"),
    # companiesCount upper bound (1517) -- proves INTEGER is sufficient
    ("financial-services", "US"),
    # Region coverage: topETFs empty, topCompanies entirely different;
    # overview/performance identical to US
    ("technology", "GB"),
)
DOMAIN_INDUSTRY_FIXTURES: tuple[tuple[str, str], ...] = (
    # Both mover lists populated, sectorKey link, no industriesCount
    ("semiconductors", "US"),
    # None of the three list blocks present (companiesCount=1)
    ("infrastructure-operations", "US"),
    # ytdReturn = 9999.0, not a sentinel
    ("biotechnology", "US"),
    # Same symbol in both lists; growthEstimate / name missing
    ("pharmaceutical-retailers", "US"),
    # growthEstimate upper extreme
    ("electronic-components", "US"),
    # name missing in topPerforming
    ("gold", "US"),
)


def capture_domain() -> None:
    """Saves raw JSON envelopes ({"data": {...}}) as fixtures.

    Stored as-is: `fetch_domain` reads `payload["data"]`, and an unwrapped
    fixture could never exercise the `KeyError('data')` -> failed path."""
    from yfin.datasets.domain.common import _QUERY

    root = FIXTURE_ROOT / "_domain"
    specs = (
        ("sector", "sectors", DOMAIN_SECTOR_FIXTURES),
        ("industry", "industries", DOMAIN_INDUSTRY_FIXTURES),
    )
    for kind, path, entries in specs:
        target = root / kind
        target.mkdir(parents=True, exist_ok=True)
        for key, region in entries:
            payload = _yf_data().get_raw_json(
                f"{_QUERY}/{path}/{key}",
                params={
                    "formatted": "true",
                    "withReturns": "true",
                    "lang": "en-US",
                    "region": region,
                },
            )
            suffix = "" if region == "US" else f".{region}"
            file = target / f"{key}{suffix}.json"
            file.write_text(nz.canonical_json(payload), encoding="utf-8")
            print(f"_domain/{kind}/{file.name}  ({file.stat().st_size} byte)")


# --- discovery and screen fixtures ---------------------------------------
# Each entry proves one edge case on its own; removing one silently drops
# coverage for that case.
DISCOVERY_SEARCH_FIXTURES = (
    # symbol-bearing row + a symbol-less Crunchbase row together
    ("AAPL", "search_AAPL"),
    # `lists` populated; two rows of the same EQUITY type with 12 and 16
    # keys; prevName + nameChangeDate
    ("GC=F", "search_GC=F"),
    # 0 quotes but populated news + reports: `search_quotes` empty while
    # the cell is `ok`
    ("Turkish Airlines", "search_Turkish-Airlines"),
    # all blocks empty -> gate row not written
    ("zzzqqxnope", "search_zzzqqxnope"),
    # CRYPTOCURRENCY: narrow field set, no sector/industry
    ("BTC-USD", "search_BTC-USD"),
    # `lists` in two shapes: ALGO_WATCHLIST + PREDEFINED_SCREENER
    ("gold", "search_lists_two_shapes"),
)

DISCOVERY_LOOKUP_FIXTURES = (
    # narrow term: `all` returns the full set, 9-type lookupTotals,
    # includes privateCompany
    ("BTC", "all", "lookup_BTC_all"),
    # broad term: `all` truncates around 1,000 (996 documents / 7,261
    # total) -- proves the adaptive branch
    ("GOLD", "all", "lookup_GOLD_all"),
    # typed call for the same term: industryLink/industryName appear only
    # here
    ("GOLD", "equity", "lookup_GOLD_equity"),
    # total=0, no error -> `empty`
    ("zzzqqxnope", "all", "lookup_zzzqqxnope"),
)

# (screen, offset, size, file). Page size is kept small: enough to prove
# structure for normalize tests, while a 250-row page fixture would be
# megabytes. `total` comes back independent of page size, so the stop
# condition is still tested against the real value.
SCREEN_FIXTURES = (
    # predefined GET: 17 keys, metadata populated
    ("day_gainers", None, 25, "day_gainers_p0"),
    ("top_mutual_funds", None, 25, "top_mutual_funds_p0"),
    # POST: 5 keys, no metadata
    ("top_mutual_funds", 25, 25, "top_mutual_funds_p1"),
    # last page: where the offset + len < total condition ends
    ("top_mutual_funds", 1750, 250, "top_mutual_funds_last"),
    # mixed quoteType (EQUITY + ETF) in one screen
    ("bond_etfs", None, 25, "bond_etfs_p0"),
)


def capture_discovery() -> None:
    """Saves raw `Search` and `Lookup` response bodies.

    `include_research=True` and `include_nav_links=True` are explicit: both
    default to False, and without them `researchReports` never comes back."""
    from yfin.ingest.screens import SCREEN_KEY_MAX_LENGTH  # noqa: F401  (import check)

    target = FIXTURE_ROOT / "_discovery"
    target.mkdir(parents=True, exist_ok=True)

    for query, name in DISCOVERY_SEARCH_FIXTURES:
        search = yf.Search(
            query,
            max_results=10,
            news_count=5,
            lists_count=10,
            include_research=True,
            include_nav_links=True,
        )
        file = target / f"{name}.json"
        file.write_text(nz.canonical_json(search.response), encoding="utf-8")
        print(f"_discovery/{file.name}  ({file.stat().st_size} byte)")

    for query, lookup_type, name in DISCOVERY_LOOKUP_FIXTURES:
        # `_fetch_lookup` is the wrapper's own method, and the only path
        # that carries the `lookupTotals` + `total` fields thrown in by
        # `_parse_response` (lookup.py:96-104).
        payload = yf.Lookup(query)._fetch_lookup(lookup_type, 1000)
        file = target / f"{name}.json"
        file.write_text(nz.canonical_json(payload), encoding="utf-8")
        print(f"_discovery/{file.name}  ({file.stat().st_size} byte)")


def capture_screen() -> None:
    """Saves `yf.screen` responses.

    The first page is requested with `count`, later pages with `size`: Yahoo
    silently ignores `count` sent together with `offset`."""
    from yfin.ingest.screens import screen_by_key

    target = FIXTURE_ROOT / "_screen"
    target.mkdir(parents=True, exist_ok=True)

    for key, offset, size, name in SCREEN_FIXTURES:
        spec = screen_by_key(key)
        if offset is None:
            payload = yf.screen(
                key, count=size, sortField=spec.sort_field, sortAsc=spec.sort_asc
            )
        else:
            payload = yf.screen(
                key, offset=offset, size=size, sortField=spec.sort_field, sortAsc=spec.sort_asc
            )
        file = target / f"{name}.json"
        file.write_text(nz.canonical_json(payload), encoding="utf-8")
        print(f"_screen/{file.name}  ({file.stat().st_size} byte)")

    # Custom screen: even the first page is POST, and no metadata comes back.
    spec = screen_by_key("tr_equity")
    assert spec.query is not None
    payload = yf.screen(
        spec.query, size=25, sortField=spec.sort_field, sortAsc=spec.sort_asc
    )
    file = target / "tr_equity_p0.json"
    file.write_text(nz.canonical_json(payload), encoding="utf-8")
    print(f"_screen/{file.name}  ({file.stat().st_size} byte)")


def _yf_data() -> Any:
    from yfinance.data import YfData

    return YfData()


def _write(target: Path, data: dict[str, Any]) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for dataset, payload in data.items():
        path = target / f"{dataset}.json"
        path.write_text(nz.canonical_json(payload), encoding="utf-8")
        print(f"{target.name}/{dataset}.json  ({path.stat().st_size} byte)")


def main(symbols: list[str]) -> None:
    if symbols == ["_market"]:
        _write(FIXTURE_ROOT / "_market", capture_market())
        return
    if symbols and symbols[0] == "--domain":
        capture_domain()
        return
    if symbols and symbols[0] == "--discovery":
        capture_discovery()
        return
    if symbols and symbols[0] == "--screen":
        capture_screen()
        return
    if symbols and symbols[0] == "--bars":
        # Bar fixtures only: 7 requests instead of re-fetching everything
        # (12 symbols x 7 requests).
        for symbol in symbols[1:] or list(BAR_FIXTURE_SYMBOLS):
            _write(FIXTURE_ROOT / symbol, capture_bars(symbol))
        _write(FIXTURE_ROOT / "AAPL", capture_weekly("AAPL"))
        return
    for symbol in symbols or list(REFERENCE_SYMBOLS):
        _write(FIXTURE_ROOT / symbol, capture(symbol))


if __name__ == "__main__":
    main(sys.argv[1:])
