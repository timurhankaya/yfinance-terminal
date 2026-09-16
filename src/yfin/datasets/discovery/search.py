"""search dataset.

Ungated: symbols, news, news_symbols, research_reports. Gated: search_quotes,
search_lists, search_report_hits. `include_research` must be passed explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import yfinance as yf

from yfin.core import normalize as nz
from yfin.core.config import get_settings
from yfin.core.families import DataFamily
from yfin.core.logging_setup import get_logger
from yfin.datasets.asof_base import asof_produces
from yfin.datasets.base import NormalizedResult, SyncContext
from yfin.datasets.discovery.base import DISCOVERY_GATE_TABLE, DiscoveryDataset
from yfin.datasets.exposure import ApiExposure
from yfin.datasets.news import _thumbnail
from yfin.datasets.registry import register
from yfin.datasets.symbol_discovery import (
    dict_items,
    discovered_symbol_row,
    expect_dict,
    symbol_is_writable,
    utc_as_of_day,
)
from yfin.ingest.client import call_yahoo
from yfin.storage.contracts import TableWrite

log = get_logger(__name__)

# Only the columns Search populates: a full update would NULL the richer
# body `Ticker.news` writes for the same item.
NEWS_UPDATE = (
    "title",
    "pub_date",
    "provider_name",
    "click_through_url",
    "content_type",
)

_QUOTE_UPDATE = (
    "rank_index",
    "score",
    "quote_type",
    "type_disp",
    "exchange",
    "exch_disp",
    "short_name",
    "long_name",
    "sector",
    "sector_disp",
    "industry",
    "industry_disp",
    "disp_sec_ind_flag",
    "is_yahoo_finance",
    "prev_name",
    "name_change_date",
    "is_known",
    "fetched_at",
    "raw_json",
)

_LIST_UPDATE = (
    "rank_index",
    "list_type",
    "name",
    "score",
    "icon_url",
    "brand_slug",
    "pf_id",
    "user_id",
    "symbol_count",
    "daily_percent_gain",
    "follower_count",
    "yahoo_id",
    "total",
    "is_premium",
    "fetched_at",
    "raw_json",
)

_REPORT_UPDATE = ("provider", "author", "report_headline", "report_ts_utc", "fetched_at")
_HIT_UPDATE = ("rank_index", "fetched_at")

# Identifying columns search populates. Narrower than `screener`'s: a
# Search quote carries no `currency`/`timezone`/`firstTradeDate`.
SYMBOL_UPDATE = ("short_name", "long_name", "exchange", "quote_type", "last_seen_at")

REPORT_ID_MAX = 64


@dataclass
class SearchPayload:
    query_term: str
    as_of_date: date
    fetched_at: datetime
    quotes: list[dict[str, Any]]
    news: list[dict[str, Any]]
    lists: list[dict[str, Any]]
    reports: list[dict[str, Any]]


class SearchDataset(DiscoveryDataset[SearchPayload]):
    name = "search"
    produces = asof_produces(
        "symbols",
        "news",
        "news_symbols",
        "research_reports",
        "search_quotes",
        "search_lists",
        "search_report_hits",
        gate=DISCOVERY_GATE_TABLE,
    )
    # All three, because none of them alone is guaranteed: "Turkish
    # Airlines" comes back with no quotes and three research reports, so
    # `search_report_hits` is the only gated table with a row. The four
    # ungated tables are never sources -- they carry no `query_term`.
    gate_source_tables = ("search_quotes", "search_report_hits", "search_lists")
    api = (
        ApiExposure(
            name="search_quotes",
            family=DataFamily.DISCOVERY,
            table="search_quotes",
            sort_key=("as_of_date", "query_term", "symbol"),
            descending=True,
            filters=("query_term",),
            symbol_optional=True,
            description="Symbols a free-text search returned.",
        ),
        ApiExposure(
            name="search_lists",
            family=DataFamily.DISCOVERY,
            table="search_lists",
            sort_key=("as_of_date", "query_term", "list_key"),
            descending=True,
            filters=("query_term",),
            description="Curated lists a search returned.",
        ),
        ApiExposure(
            name="search_report_hits",
            family=DataFamily.DISCOVERY,
            table="search_report_hits",
            sort_key=("as_of_date", "query_term", "report_id"),
            descending=True,
            filters=("query_term",),
            description="Research reports a search returned.",
        ),
    )

    def fetch(self, ctx: SyncContext) -> SearchPayload:
        cfg = get_settings()
        term = ctx.symbol
        raw = call_yahoo(
            lambda: yf.Search(
                term,
                max_results=cfg.yf_search_max_results,
                news_count=cfg.yf_search_news_count,
                lists_count=cfg.yf_search_lists_count,
                # Passed explicitly -- the default is False.
                include_research=True,
                # `nav` is out of scope: both fields are UI navigation
                # links. Requesting it also adds the `timeTakenForNav` cost.
                include_nav_links=False,
            ).response,
            what=f"search:{term}",
        )
        body = expect_dict(raw, what="search")
        return SearchPayload(
            query_term=term,
            as_of_date=utc_as_of_day(ctx.fetched_at),
            fetched_at=ctx.fetched_at,
            quotes=dict_items(body, "quotes"),
            news=dict_items(body, "news"),
            lists=dict_items(body, "lists"),
            reports=dict_items(body, "researchReports"),
        )

    def normalize(self, raw: SearchPayload, symbol: str) -> NormalizedResult:
        quotes, symbols = _quote_rows(raw)
        news, news_symbols = _news_rows(raw)
        reports, hits = _report_rows(raw)

        return NormalizedResult(
            writes=[
                # --- ungated ---
                TableWrite(
                    table="symbols",
                    rows=symbols,
                    key_columns=("symbol",),
                    update_columns=SYMBOL_UPDATE,
                ),
                TableWrite(
                    table="news",
                    rows=news,
                    key_columns=("news_id",),
                    update_columns=NEWS_UPDATE,
                ),
                TableWrite(
                    table="news_symbols",
                    rows=news_symbols,
                    key_columns=("news_id", "symbol"),
                    update_columns=("is_known",),
                ),
                TableWrite(
                    table="research_reports",
                    rows=reports,
                    key_columns=("report_id",),
                    update_columns=_REPORT_UPDATE,
                ),
                # --- gated ---
                TableWrite(
                    table="search_quotes",
                    rows=quotes,
                    key_columns=("query_term", "as_of_date", "symbol"),
                    update_columns=_QUOTE_UPDATE,
                ),
                TableWrite(
                    table="search_lists",
                    rows=_list_rows(raw),
                    key_columns=("query_term", "as_of_date", "list_key"),
                    update_columns=_LIST_UPDATE,
                ),
                TableWrite(
                    table="search_report_hits",
                    rows=hits,
                    key_columns=("query_term", "as_of_date", "report_id"),
                    update_columns=_HIT_UPDATE,
                ),
            ]
        )


def _quote_rows(raw: SearchPayload) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    symbols: list[dict[str, Any]] = []
    seen: set[str] = set()
    rank_index = 0

    for quote in raw.quotes:
        symbol = nz.to_str(quote.get("symbol"))
        if symbol is None:
            # Crunchbase private-company record. `rank_index` does not
            # advance -- rank counts over written rows, otherwise the
            # 0-based dense sequence would break.
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        is_known = symbol_is_writable(symbol)
        rows.append(
            {
                "query_term": raw.query_term,
                "as_of_date": raw.as_of_date,
                "symbol": symbol,
                "rank_index": rank_index,
                "score": nz.to_decimal(quote.get("score")),
                "quote_type": nz.to_str(quote.get("quoteType"), max_len=32),
                "type_disp": nz.to_str(quote.get("typeDisp"), max_len=64),
                "exchange": nz.to_str(quote.get("exchange"), max_len=32),
                "exch_disp": nz.to_str(quote.get("exchDisp"), max_len=64),
                "short_name": nz.to_str(quote.get("shortname"), max_len=128),
                "long_name": nz.to_str(quote.get("longname"), max_len=255),
                # Sector/industry family is populated for equity rows only
                "sector": nz.to_str(quote.get("sector"), max_len=64),
                "sector_disp": nz.to_str(quote.get("sectorDisp"), max_len=64),
                "industry": nz.to_str(quote.get("industry"), max_len=128),
                "industry_disp": nz.to_str(quote.get("industryDisp"), max_len=128),
                "disp_sec_ind_flag": nz.to_bool(quote.get("dispSecIndFlag")),
                "is_yahoo_finance": nz.to_bool(quote.get("isYahooFinance")),
                # Source carries leading whitespace; `nz.to_str` trims it
                "prev_name": nz.to_str(quote.get("prevName"), max_len=255),
                "name_change_date": nz.to_datetime_utc(quote.get("nameChangeDate")),
                "is_known": is_known,
                "fetched_at": raw.fetched_at,
                "raw_json": nz.canonical_json(quote),
            }
        )
        rank_index += 1
        if is_known:
            symbols.append(
                discovered_symbol_row(
                    symbol,
                    source="search",
                    fetched_at=raw.fetched_at,
                    # A Search quote carries no currency/timezone/firstTradeDate;
                    # `SYMBOL_UPDATE` is limited to these four fields.
                    short_name=nz.to_str(quote.get("shortname"), max_len=128),
                    long_name=nz.to_str(quote.get("longname"), max_len=255),
                    exchange=nz.to_str(quote.get("exchange"), max_len=32),
                    quote_type=nz.to_str(quote.get("quoteType"), max_len=32),
                )
            )
    return rows, symbols


def _list_rows(raw: SearchPayload) -> list[dict[str, Any]]:
    """One table for two shapes: ALGO_WATCHLIST (slug/name/symbolCount) and
    PREDEFINED_SCREENER (canonicalName/title/total).
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    rank_index = 0
    for entry in raw.lists:
        key = nz.to_str(entry.get("slug")) or nz.to_str(entry.get("canonicalName"))
        if key is None or len(key) > 128 or key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "query_term": raw.query_term,
                "as_of_date": raw.as_of_date,
                "list_key": key,
                "rank_index": rank_index,
                "list_type": nz.to_str(entry.get("type"), max_len=32),
                "name": nz.to_str(entry.get("name") or entry.get("title"), max_len=255),
                "score": nz.to_decimal(entry.get("score")),
                "icon_url": nz.to_str(entry.get("iconUrl")),
                "brand_slug": nz.to_str(entry.get("brandSlug"), max_len=64),
                "pf_id": nz.to_str(entry.get("pfId"), max_len=128),
                "user_id": nz.to_str(entry.get("userId"), max_len=64),
                "symbol_count": nz.to_int(entry.get("symbolCount")),
                "daily_percent_gain": nz.to_decimal(entry.get("dailyPercentGain")),
                "follower_count": nz.to_int(entry.get("followerCount")),
                "yahoo_id": nz.to_str(entry.get("id"), max_len=64),
                "total": nz.to_int(entry.get("total")),
                "is_premium": nz.to_bool(entry.get("isPremium")),
                "fetched_at": raw.fetched_at,
                "raw_json": nz.canonical_json(entry),
            }
        )
        rank_index += 1
    return rows


def _news_rows(raw: SearchPayload) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Same PK as `Ticker.news`, narrower body."""
    rows: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in raw.news:
        news_id = nz.to_str(item.get("uuid"))
        title = nz.to_str(item.get("title"), max_len=512)
        pub_date = nz.epoch_to_datetime(item.get("providerPublishTime"), unit="s")
        if news_id is None or title is None or pub_date is None:
            # `title` and `pub_date` are NOT NULL; if missing, the row is
            # dropped and a warning logged -- instead of dropping the whole cell.
            log.warning("a search news item arrived with a missing field", news_id=news_id)
            continue
        if news_id in seen:
            continue
        seen.add(news_id)
        thumb_url, thumb_w, thumb_h = _thumbnail(item.get("thumbnail"))
        rows.append(
            {
                "news_id": news_id,
                "title": title,
                "summary": None,
                "description": None,
                "content_type": nz.to_str(item.get("type"), max_len=32),
                "pub_date": pub_date,
                "display_time": None,
                "provider_name": nz.to_str(item.get("publisher"), max_len=128),
                "provider_url": None,
                "provider_source_id": None,
                "canonical_url": None,
                "click_through_url": nz.to_str(item.get("link")),
                "thumbnail_url": thumb_url,
                "thumbnail_width": thumb_w,
                "thumbnail_height": thumb_h,
                "raw_json": nz.canonical_json(item),
            }
        )
        for ticker in item.get("relatedTickers") or []:
            symbol = nz.to_str(ticker)
            if symbol is None or not symbol_is_writable(symbol):
                continue
            links.append({"news_id": news_id, "symbol": symbol, "is_known": False})
    return rows, links


def _report_rows(raw: SearchPayload) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Shared entity plus link.

    `reportDate` is epoch milliseconds here but ISO text on the domain
    path, so the converters cannot be shared.
    """
    reports: list[dict[str, Any]] = []
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    rank_index = 0

    for entry in raw.reports:
        report_id = nz.to_str(entry.get("id"))
        if report_id is None or len(report_id) > REPORT_ID_MAX or report_id in seen:
            continue
        seen.add(report_id)
        reports.append(
            {
                "report_id": report_id,
                "as_of_date": raw.as_of_date,
                "provider": nz.to_str(entry.get("provider"), max_len=64),
                "report_type": None,
                "head_html": None,
                "report_title": None,
                "target_price": None,
                "target_price_status": None,
                "investment_rating": None,
                "report_ts_utc": nz.epoch_to_datetime(entry.get("reportDate"), unit="ms"),
                "author": nz.to_str(entry.get("author"), max_len=128),
                "report_headline": nz.to_str(entry.get("reportHeadline"), max_len=512),
                "first_seen_at": raw.fetched_at,
                "fetched_at": raw.fetched_at,
            }
        )
        hits.append(
            {
                "query_term": raw.query_term,
                "as_of_date": raw.as_of_date,
                "report_id": report_id,
                "rank_index": rank_index,
                "fetched_at": raw.fetched_at,
            }
        )
        rank_index += 1
    return reports, hits


# Opt-in: registered but excluded from the `all` expansion.
# `yfin sync --datasets search` runs it; a bare `yfin sync` does not fetch
# it, so its cost is unchanged.
register(SearchDataset(), opt_in=True)
