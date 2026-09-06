"""search dataset'i (SQ S7.1).

-> symbols | news | news_symbols | research_reports        [kapi disi]
-> search_quotes | search_lists | search_report_hits       [kapili]

UC OLCULMUS TUZAK bu modulun seklini belirledi:

1. `include_research` VARSAYILANI FALSE'tur (search.py:32-34). Acikca
   verilmezse `researchReports` HIC gelmez ve iki tablo sessizce bos
   kalirdi -- ustelik S9.6 eksiksizlik kaniti bunu YAKALAMAZDI, cunku
   "kaynak bos dondu" ile "istemedik" ayni gorunurdu.

2. `quotes` blogu SEMBOLSUZ satir tasir. `include_cb=True` varsayilani
   Crunchbase ozel-sirket kayitlarini getiriyor
   (`{index, name, permalink, isYahooFinance}`). `yfinance`in `.quotes`
   ozelligi bunlari suzuyor (search.py:110) ama biz `.response` ham
   govdesini kullaniyoruz -- suzgec BURADA acikca uygulanir (SQ K14).

3. Search haberi `Ticker.news` ILE AYNI KIMLIK UZAYINDA ama govdesi DAR:
   8 anahtara karsi 17. Kor upsert zengin satiri NULL'lardi; bu yuzden
   `update_columns` yalnizca GERCEKTEN doldurulan kolonlarla sinirlidir
   (SQ S8.5).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import yfinance as yf

from yfin import normalize as nz
from yfin.client import call_yahoo
from yfin.config import get_settings
from yfin.datasets.asof_base import asof_produces
from yfin.datasets.base import NormalizedResult, SyncContext, TableWrite
from yfin.datasets.discovery.base import DISCOVERY_GATE_TABLE, DiscoveryDataset
from yfin.datasets.news import _thumbnail
from yfin.datasets.registry import register
from yfin.logging_setup import get_logger

log = get_logger(__name__)

# SQ S8.5: Search'un GERCEKTEN doldurdugu kolonlar. Kapsam disinda kalan
# `summary`, `description`, `canonical_url`, `provider_url`,
# `provider_source_id`, `display_time`, `thumbnail_*` ve `raw_json`
# INSERT'te yazilir, sonraki Search gecislerinde DOKUNULMAZ. Boylece
# `Ticker.news`in zengin govdesi daima fakiri ezer, tersi ASLA olmaz.
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

# SQ S5.12: search'un doldurdugu tanimlayici kolonlar. `screener`inkinden
# DAR: Search kotasyonu `currency`/`timezone`/`firstTradeDate` tasimaz.
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

    def fetch(self, ctx: SyncContext) -> SearchPayload:
        cfg = get_settings()
        term = ctx.symbol
        raw = call_yahoo(
            lambda: yf.Search(
                term,
                max_results=cfg.yf_search_max_results,
                news_count=cfg.yf_search_news_count,
                lists_count=cfg.yf_search_lists_count,
                # ACIKCA verilir -- varsayilani False (SQ S4.1/5).
                include_research=True,
                # `nav` kapsam disi (SQ S1): iki alan, ikisi de UI
                # navigasyon baglantisi. Istemek `timeTakenForNav`
                # maliyetini de ekler.
                include_nav_links=False,
            ).response,
            what=f"search:{term}",
        )
        if not isinstance(raw, dict):  # pragma: no cover - savunma
            raise TypeError(f"search yaniti sozluk degil: {type(raw).__name__}")

        return SearchPayload(
            query_term=term,
            as_of_date=datetime.now(UTC).date(),
            fetched_at=ctx.fetched_at,
            quotes=[q for q in raw.get("quotes") or [] if isinstance(q, dict)],
            news=[n for n in raw.get("news") or [] if isinstance(n, dict)],
            lists=[x for x in raw.get("lists") or [] if isinstance(x, dict)],
            reports=[r for r in raw.get("researchReports") or [] if isinstance(r, dict)],
        )

    def normalize(self, raw: SearchPayload, symbol: str) -> NormalizedResult:
        quotes, symbols = _quote_rows(raw)
        news, news_symbols = _news_rows(raw)
        reports, hits = _report_rows(raw)

        return NormalizedResult(
            writes=[
                # --- kapi disi (SQ S6.2.1) ---
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
                # --- kapili ---
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


def _symbol_is_writable(symbol: str) -> bool:
    """`^` KAPSAM ICINDEDIR: endeks sembolleri onunla baslar (SQ S8.3)."""
    return len(symbol) <= 32 and symbol.isascii()


def _quote_rows(raw: SearchPayload) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    symbols: list[dict[str, Any]] = []
    seen: set[str] = set()
    rank_index = 0

    for quote in raw.quotes:
        symbol = nz.to_str(quote.get("symbol"))
        if symbol is None:
            # SQ K14: Crunchbase ozel-sirket kaydi. `rank_index` ARTMAZ -- sira
            # yazilan satirlar uzerinden sayilir, yoksa 0-tabanli yogun
            # dizi bozulurdu.
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        is_known = _symbol_is_writable(symbol)
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
                # Sektor/endustri ailesi YALNIZ EQUITY satirlarinda
                "sector": nz.to_str(quote.get("sector"), max_len=64),
                "sector_disp": nz.to_str(quote.get("sectorDisp"), max_len=64),
                "industry": nz.to_str(quote.get("industry"), max_len=128),
                "industry_disp": nz.to_str(quote.get("industryDisp"), max_len=128),
                "disp_sec_ind_flag": nz.to_bool(quote.get("dispSecIndFlag")),
                "is_yahoo_finance": nz.to_bool(quote.get("isYahooFinance")),
                # Bastaki bosluk olculdu; `nz.to_str` kirpar
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
                {
                    "symbol": symbol,
                    "short_name": nz.to_str(quote.get("shortname"), max_len=128),
                    "long_name": nz.to_str(quote.get("longname"), max_len=255),
                    "exchange": nz.to_str(quote.get("exchange"), max_len=32),
                    "quote_type": nz.to_str(quote.get("quoteType"), max_len=32),
                    # Yalniz INSERT'te etkili (SQ K10)
                    "is_active": False,
                    "discovered_by": "search",
                    "discovered_at": raw.fetched_at,
                    "last_seen_at": raw.fetched_at,
                }
            )
    return rows, symbols


def _list_rows(raw: SearchPayload) -> list[dict[str, Any]]:
    """IKI SEKILLI blok (SQ S4.1/6).

    `ALGO_WATCHLIST` -> `slug` + `name` + `symbolCount`
    `PREDEFINED_SCREENER` -> `canonicalName` + `title` + `total`

    Ortak alan yalnizca dorttur; ayri tablolar onlari cogaltirdi.
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
    """`Ticker.news` ile AYNI PK, DAR govde (SQ S8.5)."""
    rows: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in raw.news:
        news_id = nz.to_str(item.get("uuid"))
        title = nz.to_str(item.get("title"), max_len=512)
        pub_date = nz.epoch_to_datetime(item.get("providerPublishTime"), unit="s")
        if news_id is None or title is None or pub_date is None:
            # `title` ve `pub_date` NOT NULL; eksikse satir DUSER ve uyari
            # loglanir -- hucrenin tamamini dusurmek yerine.
            log.warning("search haberi eksik alanla geldi", news_id=news_id)
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
            if symbol is None or not _symbol_is_writable(symbol):
                continue
            links.append({"news_id": news_id, "symbol": symbol, "is_known": False})
    return rows, links


def _report_rows(raw: SearchPayload) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """PAYLASILAN varlik + bag (SQ K8).

    `reportDate` burada epoch MILISANIYE gelir; domain yolunda ISO METIN
    gelir (SQ S4.3). Ortak bir donusturucu varsayilsaydi biri sessizce
    NULL olurdu.
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


if get_settings().yf_discovery_enabled:
    register(SearchDataset())
