"""The news dataset -> news, news_symbols."""

from __future__ import annotations

from typing import Any

from yfin.core import normalize as nz
from yfin.core.config import get_settings
from yfin.core.families import DataFamily
from yfin.datasets.base import Dataset, NormalizedResult, SyncContext, mark_known_in, plain_upsert
from yfin.datasets.common import key_value
from yfin.datasets.exposure import ApiExposure
from yfin.datasets.payloads import NewsPayload
from yfin.datasets.registry import register
from yfin.ingest.client import call_yahoo, make_ticker
from yfin.storage.contracts import RowWriter, TableWrite, WriteStats

_NEWS_UPDATE = (
    "title",
    "summary",
    "description",
    "content_type",
    "pub_date",
    "display_time",
    "provider_name",
    "provider_url",
    "provider_source_id",
    "canonical_url",
    "click_through_url",
    "thumbnail_url",
    "thumbnail_width",
    "thumbnail_height",
    "raw_json",
)


def _as_dict(value: Any) -> dict[str, Any]:
    """Upstream, these fields may be a dict or None."""
    return value if isinstance(value, dict) else {}


def _url(value: Any) -> str | None:
    """canonicalUrl / clickThroughUrl is a dict carrying a 'url' key."""
    if isinstance(value, dict):
        return nz.to_str(value.get("url"))
    return nz.to_str(value)


def _thumbnail(thumb: Any) -> tuple[str | None, int | None, int | None]:
    """The tag='original' resolution goes into columns; the rest stay in
    raw_json. thumbnail may be None."""
    if not isinstance(thumb, dict):
        return None, None, None
    for res in thumb.get("resolutions") or []:
        if isinstance(res, dict) and res.get("tag") == "original":
            return (
                nz.to_str(res.get("url")),
                nz.to_int(res.get("width")),
                nz.to_int(res.get("height")),
            )
    return (
        nz.to_str(thumb.get("originalUrl")),
        nz.to_int(thumb.get("originalWidth")),
        nz.to_int(thumb.get("originalHeight")),
    )


def _iso_to_dt(value: Any) -> Any:
    """'2026-09-03T12:00:00Z' -> naive UTC datetime. An empty string is a sentinel."""
    text = nz.to_str(value)
    if text is None:
        return None
    import pandas as pd

    try:
        return nz.to_datetime_utc(pd.Timestamp(text))
    except (ValueError, TypeError):
        return None


class NewsDataset(Dataset[NewsPayload]):
    name = "news"
    depends_on = ("symbols",)
    produces = ("news", "news_symbols")
    api = (
        ApiExposure(
            name="news",
            family=DataFamily.NEWS,
            table="news",
            sort_key=("pub_date", "news_id"),
            descending=True,
            description="Articles, newest first, across the whole universe.",
        ),
        # The article and the symbols it mentions are separate rows, so
        # "news about AAPL" is two calls: this one for the ids, then the
        # articles. The generic surface does not join, and teaching it to
        # would be a query planner nobody asked for.
        ApiExposure(
            name="news_symbols",
            family=DataFamily.NEWS,
            table="news_symbols",
            sort_key=("news_id",),
            descending=True,
            description="Which symbols an article mentions.",
        ),
    )

    def fetch(self, ctx: SyncContext) -> NewsPayload:
        settings = get_settings()
        # A FRESH Ticker is used: the get_news cache ignores the count/tab
        # parameters ('if self._news: return self._news'), so if the same
        # Ticker already fetched news the call would silently return 10
        # items. This is the ONLY exception to the ctx.cached mechanism.
        ticker = make_ticker(ctx.symbol)
        result: NewsPayload = call_yahoo(
            lambda: ticker.get_news(count=settings.yf_news_count, tab=settings.yf_news_tab),
            what=f"news:{ctx.symbol}",
        )
        return result

    def normalize(self, raw: NewsPayload, symbol: str) -> NormalizedResult:
        if nz.is_empty_result(raw):
            return NormalizedResult()

        news_rows: list[dict[str, Any]] = []
        link_rows: list[dict[str, Any]] = []
        seen_news: set[str] = set()
        seen_links: set[tuple[str, str]] = set()

        for item in raw:
            if not isinstance(item, dict):
                continue
            # PK -> NEVER TRUNCATED: two articles sharing the first 36
            # characters would collapse into one row and the second
            # article's body would overwrite the first.
            news_id = key_value(
                item.get("id"), 36, field="news_id", dataset=self.name, symbol=symbol
            )
            content = item.get("content")
            if news_id is None or not isinstance(content, dict):
                continue
            title = nz.to_str(content.get("title"), 512)
            pub_date = _iso_to_dt(content.get("pubDate"))
            if title is None or pub_date is None:
                # title and pub_date are NOT NULL
                continue

            provider = _as_dict(content.get("provider"))
            thumb_url, thumb_w, thumb_h = _thumbnail(content.get("thumbnail"))

            if news_id not in seen_news:
                seen_news.add(news_id)
                news_rows.append(
                    {
                        "news_id": news_id,
                        "title": title,
                        "summary": nz.to_str(content.get("summary")),
                        "description": nz.to_str(content.get("description")),
                        "content_type": nz.to_str(content.get("contentType"), 32),
                        "pub_date": pub_date,
                        # displayTime can arrive as an empty string -> NULL
                        "display_time": _iso_to_dt(content.get("displayTime")),
                        "provider_name": nz.to_str(provider.get("displayName"), 128),
                        "provider_url": nz.to_str(provider.get("url"), 255),
                        "provider_source_id": nz.to_str(provider.get("sourceId"), 64),
                        "canonical_url": _url(content.get("canonicalUrl")),
                        "click_through_url": _url(content.get("clickThroughUrl")),
                        "thumbnail_url": thumb_url,
                        "thumbnail_width": thumb_w,
                        "thumbnail_height": thumb_h,
                        "raw_json": nz.canonical_json(item),
                    }
                )

            # An article can carry several symbols, hence the M:N link.
            finance = _as_dict(content.get("finance"))
            tickers = finance.get("stockTickers") or []
            symbols: list[str] = []
            for entry in tickers:
                candidate = entry.get("symbol") if isinstance(entry, dict) else entry
                text = key_value(
                    candidate, 32, field="linked_symbol", dataset=self.name, symbol=symbol
                )
                if text is not None:
                    symbols.append(nz.normalize_symbol(text))
            if symbol not in symbols:
                symbols.append(symbol)

            for linked in symbols:
                key = (news_id, linked)
                if key in seen_links:
                    continue
                seen_links.add(key)
                # is_known is filled from the DB during the upsert
                link_rows.append({"news_id": news_id, "symbol": linked, "is_known": False})

        if not news_rows:
            return NormalizedResult()

        return NormalizedResult(
            writes=[
                TableWrite(
                    table="news",
                    rows=news_rows,
                    key_columns=("news_id",),
                    update_columns=_NEWS_UPDATE,
                ),
                TableWrite(
                    table="news_symbols",
                    rows=link_rows,
                    key_columns=("news_id", "symbol"),
                    update_columns=("is_known",),
                ),
            ]
        )

    def upsert(
        self, writer: RowWriter, result: NormalizedResult, *, full_refresh: bool = False
    ) -> WriteStats:
        """`full_refresh` is accepted and ignored: this dataset has no gate."""
        # No FK on news_symbols.symbol: upstream sends symbols from outside
        # the universe, and one transaction per symbol means an FK
        # violation would roll back ALL of that symbol's data. The other
        # table has no such column and must not be marked.
        marked = mark_known_in(
            writer,
            result,
            select=lambda write: write.table == "news_symbols" and bool(write.rows),
        )
        return plain_upsert(writer, marked)



register(NewsDataset())
