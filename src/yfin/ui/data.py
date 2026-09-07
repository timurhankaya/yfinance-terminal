"""Read routes that exist only for the browser terminal.

The public `/v1` surface deliberately does not join: "news about AAPL" is
two calls there (`news_symbols` for the ids, then `news`). The page wants
one list, newest first, so the join lives here, outside the OpenAPI
document and outside the metered surface -- reachable by anyone, like the
rest of the terminal, with `RequestBrake` the only thing in front of it.
Promoting it to `/v1` is a separate decision (spec, "Kararlar" 8).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from yfin.api.core.errors import TYPE_INVALID_PARAMETER, ApiProblem
from yfin.api.schemas.common import Collection
from yfin.api.storage import limits
from yfin.api.storage.session import session_scope
from yfin.core.normalize import normalize_symbol
from yfin.models.news import News, NewsSymbol

router = APIRouter(prefix="/ui/api", include_in_schema=False)

#: Same shape market.py and datasets.py declare for themselves; the
#: storage module exports only the generator.
SessionDep = Annotated[Session, Depends(session_scope)]

NEWS_DEFAULT_LIMIT = 50
NEWS_MAX_LIMIT = 200


class NewsOut(BaseModel):
    news_id: str
    title: str
    summary: str | None
    pub_date: datetime
    provider_name: str | None
    #: canonical_url when the source gives one, else the click-through.
    link: str | None
    thumbnail_url: str | None


def list_news(session: Session, symbol: str, limit: int) -> list[NewsOut]:
    stmt = (
        select(News)
        .join(NewsSymbol, NewsSymbol.news_id == News.news_id)
        .where(NewsSymbol.symbol == symbol)
        .order_by(News.pub_date.desc(), News.news_id.desc())
        .limit(limit)
    )
    return [
        NewsOut(
            news_id=row.news_id,
            title=row.title,
            summary=row.summary,
            pub_date=row.pub_date,
            provider_name=row.provider_name,
            link=row.canonical_url or row.click_through_url,
            thumbnail_url=row.thumbnail_url,
        )
        for row in session.scalars(stmt)
    ]


@router.get("/symbols/{symbol}/news", response_model=Collection[NewsOut])
def symbol_news(
    session: SessionDep,
    symbol: str,
    limit: Annotated[int | None, Query(ge=1)] = None,
) -> Collection[NewsOut]:
    size = limit if limit is not None else NEWS_DEFAULT_LIMIT
    if size > NEWS_MAX_LIMIT:
        raise ApiProblem(
            422,
            TYPE_INVALID_PARAMETER,
            "Page size above the maximum",
            detail=f"limit must not exceed {NEWS_MAX_LIMIT}",
        )
    limits.apply_statement_timeout(session)
    rows = list_news(session, normalize_symbol(symbol), size)
    # as_of stays None: the news table keeps no fetch timestamp on the row.
    return Collection[NewsOut](data=rows, next_cursor=None, as_of=None)
