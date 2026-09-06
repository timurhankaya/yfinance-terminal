"""The core read endpoints.

One router rather than four files: they share the same envelope, the same
cursor handling and the same caching rules, and splitting them would put
that shared machinery either in a fifth module or in four copies.

Each endpoint declares `guard(family)` and nothing else about
authorisation: the scope required, the usage counter and the plan limits
all come from that one argument.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from yfin.api.auth.dependencies import Principal
from yfin.api.core.errors import (
    TYPE_INVALID_CURSOR,
    TYPE_INVALID_PARAMETER,
    TYPE_NOT_FOUND,
    TYPE_RANGE_TOO_LARGE,
    ApiProblem,
)
from yfin.api.ratelimit.dependencies import guard
from yfin.api.schemas.common import DEFAULT_PAGE_SIZE, Collection, Resource
from yfin.api.schemas.market import (
    Action,
    Bar,
    FinancialFactOut,
    SymbolDetail,
    SymbolSummary,
)
from yfin.api.storage import cursor as cursors
from yfin.api.storage import limits, reads
from yfin.api.storage.session import session_scope
from yfin.core.families import DataFamily
from yfin.models import READABLE_INTERVALS

router = APIRouter(prefix="/v1", tags=["market"])

SessionDep = Annotated[Session, Depends(session_scope)]

#: Historic data does not change; today's does. Two values rather than a
#: formula because the difference is what matters to a client, not the
#: precise number.
CACHE_SETTLED_SECONDS = 86_400
CACHE_LIVE_SECONDS = 60


def _page_size(request: Request, requested: int | None) -> int:
    """The effective page size, refusing rather than silently clipping.

    Silently returning fewer rows than asked for looks to a client like
    the end of the data.
    """
    cap = int(getattr(request.state, "page_size_cap", DEFAULT_PAGE_SIZE))
    if requested is None:
        return min(DEFAULT_PAGE_SIZE, cap)
    if requested > cap:
        raise ApiProblem(
            422,
            TYPE_INVALID_PARAMETER,
            "Page size above the plan's maximum",
            detail=f"limit must not exceed {cap}",
        )
    return requested


def _decode(cursor: str | None, *, query: dict[str, Any], arity: int) -> tuple[Any, ...] | None:
    if cursor is None:
        return None
    try:
        return cursors.decode(cursor, query=query, arity=arity)
    except cursors.InvalidCursor as exc:
        raise ApiProblem(
            422, TYPE_INVALID_CURSOR, "The cursor is not usable here", detail=str(exc)
        ) from exc


def _finish(
    response: Response,
    *,
    request: Request,
    as_of: datetime | None,
    settled: bool,
    payload_key: str,
) -> None:
    """Cache headers and the freshness stamp.

    `private` always: these responses vary by scope and by the plan's page
    size, so a shared cache holding one and serving it to another client
    would be a data leak, not just a stale answer. `Vary: Authorization`
    says the same thing to caches that only read headers.
    """
    max_age = CACHE_SETTLED_SECONDS if settled else CACHE_LIVE_SECONDS
    response.headers["Cache-Control"] = f"private, max-age={max_age}"
    response.headers["Vary"] = "Authorization, Accept-Encoding"
    # Derived from the full request identity plus freshness, so two
    # different pages of the same query never share an ETag.
    stamp = as_of.isoformat() if as_of else "-"
    digest = hashlib.sha256(f"{payload_key}|{stamp}".encode()).hexdigest()[:32]
    response.headers["ETag"] = f'W/"{digest}"'
    if as_of is not None:
        response.headers["X-Data-As-Of"] = as_of.isoformat()


def _normalise_symbol(symbol: str) -> str:
    """Symbol columns are COLLATE "C", so `aapl` and `AAPL` are different
    values in the database. Normalising at the boundary keeps that from
    becoming the caller's problem."""
    return symbol.strip().upper()


# --- symbols ----------------------------------------------------------------


@router.get("/symbols", response_model=Collection[SymbolSummary], summary="List symbols")
def list_symbols(
    request: Request,
    response: Response,
    session: SessionDep,
    principal: Annotated[Principal, Depends(guard(DataFamily.REFERENCE))],
    exchange: Annotated[str | None, Query(max_length=limits.MAX_PARAM_LENGTH)] = None,
    quote_type: Annotated[str | None, Query(max_length=limits.MAX_PARAM_LENGTH)] = None,
    q: Annotated[
        str | None,
        Query(
            min_length=limits.MIN_PREFIX_LENGTH,
            max_length=limits.MAX_PREFIX_LENGTH,
            description="Symbol prefix. Matches the symbol column only.",
        ),
    ] = None,
    active: bool = True,
    limit: Annotated[int | None, Query(ge=1)] = None,
    cursor: str | None = None,
) -> Collection[SymbolSummary]:
    """Symbols in the universe.

    `active` defaults to true: an inactive row is one discovery found but
    an operator never activated, so the pipeline does not fetch it and it
    is close to empty.
    """
    limits.apply_statement_timeout(session)
    size = _page_size(request, limit)
    identity = {
        "route": "symbols",
        "exchange": exchange,
        "quote_type": quote_type,
        "q": q,
        "active": active,
        "limit": size,
    }
    after = _decode(cursor, query=identity, arity=1)

    page = reads.list_symbols(
        session,
        exchange=exchange,
        quote_type=quote_type,
        prefix=q,
        active=active,
        limit=size,
        after=after,
    )
    _finish(
        response,
        request=request,
        as_of=None,
        settled=False,
        payload_key=f"{identity}|{cursor}",
    )
    return Collection[SymbolSummary](
        data=[SymbolSummary(**row) for row in page.rows],
        next_cursor=(
            cursors.encode(page.next_key, query=identity) if page.next_key else None
        ),
    )


@router.get(
    "/symbols/{symbol}",
    response_model=Resource[SymbolDetail],
    summary="One symbol with its latest identity snapshot",
)
def get_symbol(
    request: Request,
    response: Response,
    session: SessionDep,
    symbol: str,
    principal: Annotated[Principal, Depends(guard(DataFamily.REFERENCE))],
) -> Resource[SymbolDetail]:
    limits.apply_statement_timeout(session)
    row = reads.get_symbol(session, _normalise_symbol(symbol))
    if row is None:
        raise ApiProblem(404, TYPE_NOT_FOUND, "No such symbol")
    _finish(
        response, request=request, as_of=None, settled=False, payload_key=f"symbol|{symbol}"
    )
    return Resource[SymbolDetail](data=SymbolDetail(**row))


# --- bars -------------------------------------------------------------------


@router.get(
    "/symbols/{symbol}/bars",
    response_model=Collection[Bar],
    summary="Price bars",
)
def list_bars(
    request: Request,
    response: Response,
    session: SessionDep,
    symbol: str,
    principal: Annotated[Principal, Depends(guard(DataFamily.BARS))],
    interval: str = "1d",
    start: Annotated[datetime | None, Query(alias="from")] = None,
    end: Annotated[datetime | None, Query(alias="to")] = None,
    session_kind: Annotated[
        Literal["regular", "all"] | None, Query(alias="session")
    ] = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
    cursor: str | None = None,
) -> Collection[Bar]:
    """Bars for one symbol.

    `session` applies to intraday intervals only, and defaults to
    `regular`. That default is accident prevention: extended-hours bars
    mixed into a regular series corrupt every indicator computed from it,
    invisibly. Above daily the flag has no meaning, so passing it there is
    rejected rather than ignored.
    """
    limits.apply_statement_timeout(session)
    code = _normalise_symbol(symbol)

    if interval not in READABLE_INTERVALS:
        raise ApiProblem(
            422,
            TYPE_INVALID_PARAMETER,
            "Unsupported interval",
            detail=f"interval must be one of: {', '.join(READABLE_INTERVALS)}",
        )

    is_intraday = limits.span_class(interval) == "intraday"
    if session_kind is not None and not is_intraday:
        raise ApiProblem(
            422,
            TYPE_INVALID_PARAMETER,
            "session does not apply to this interval",
            detail="the session filter is only meaningful for intraday intervals",
        )
    effective_session = (session_kind or reads.SESSION_REGULAR) if is_intraday else reads.SESSION_ALL

    window_start, window_end = limits.resolve_window(
        interval=interval,
        start=limits.to_utc(start),
        end=limits.to_utc(end),
        now=datetime.now(UTC),
    )
    problem = limits.window_error(interval, window_start, window_end)
    if problem is not None:
        raise ApiProblem(
            422,
            TYPE_RANGE_TOO_LARGE if "exceeds" in problem else TYPE_INVALID_PARAMETER,
            "Unacceptable time range",
            detail=problem,
        )

    if not reads.symbol_exists(session, code):
        raise ApiProblem(404, TYPE_NOT_FOUND, "No such symbol")

    size = _page_size(request, limit)
    identity = {
        "route": "bars",
        "symbol": code,
        "interval": interval,
        "from": window_start.isoformat(),
        "to": window_end.isoformat(),
        "session": effective_session,
        "limit": size,
    }
    after = _decode(cursor, query=identity, arity=1)

    page = reads.list_bars(
        session,
        symbol=code,
        interval=interval,
        start=window_start,
        end=window_end,
        session_kind=effective_session,
        limit=size,
        after=after,
    )

    _finish(
        response,
        request=request,
        as_of=None,
        settled=window_end < datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0),
        payload_key=f"{identity}|{cursor}",
    )
    return Collection[Bar](
        data=[_bar(row) for row in page.rows],
        next_cursor=(
            cursors.encode(page.next_key, query=identity) if page.next_key else None
        ),
    )


def _bar(row: dict[str, Any]) -> Bar:
    return Bar(
        symbol=row["symbol"],
        ts_utc=row["ts_utc"],
        bar_interval=row.get("bar_interval"),
        session_date=row.get("session_date"),
        local_date=row.get("local_date"),
        open=reads.to_number(row.get("open")),
        high=reads.to_number(row.get("high")),
        low=reads.to_number(row.get("low")),
        close=reads.to_number(row.get("close")),
        adj_close=reads.to_number(row.get("adj_close")),
        volume=row.get("volume"),
        is_extended=row.get("is_extended"),
    )


# --- corporate actions ------------------------------------------------------


@router.get(
    "/symbols/{symbol}/actions",
    response_model=Collection[Action],
    summary="Dividends, splits and capital gains",
)
def list_actions(
    request: Request,
    response: Response,
    session: SessionDep,
    symbol: str,
    principal: Annotated[Principal, Depends(guard(DataFamily.BARS))],
    start: Annotated[datetime | None, Query(alias="from")] = None,
    end: Annotated[datetime | None, Query(alias="to")] = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
    cursor: str | None = None,
) -> Collection[Action]:
    limits.apply_statement_timeout(session)
    code = _normalise_symbol(symbol)

    window_start, window_end = limits.resolve_window(
        interval="1mo",
        start=limits.to_utc(start),
        end=limits.to_utc(end),
        now=datetime.now(UTC),
    )
    problem = limits.window_error("1mo", window_start, window_end)
    if problem is not None:
        raise ApiProblem(422, TYPE_RANGE_TOO_LARGE, "Unacceptable time range", detail=problem)

    if not reads.symbol_exists(session, code):
        raise ApiProblem(404, TYPE_NOT_FOUND, "No such symbol")

    size = _page_size(request, limit)
    identity = {
        "route": "actions",
        "symbol": code,
        "from": window_start.isoformat(),
        "to": window_end.isoformat(),
        "limit": size,
    }
    after = _decode(cursor, query=identity, arity=2)

    page = reads.list_actions(
        session,
        symbol=code,
        start=window_start,
        end=window_end,
        limit=size,
        after=after,
    )
    _finish(
        response,
        request=request,
        as_of=None,
        settled=True,
        payload_key=f"{identity}|{cursor}",
    )
    return Collection[Action](
        data=[
            Action(
                symbol=row["symbol"],
                action_date=row["action_date"],
                action_type=row["action_type"],
                action_value=reads.to_number(row["action_value"]) or "0",
            )
            for row in page.rows
        ],
        next_cursor=(
            cursors.encode(page.next_key, query=identity) if page.next_key else None
        ),
    )


# --- financials -------------------------------------------------------------


@router.get(
    "/symbols/{symbol}/financials",
    response_model=Collection[FinancialFactOut],
    summary="Financial statement line items",
)
def list_financials(
    request: Request,
    response: Response,
    session: SessionDep,
    symbol: str,
    principal: Annotated[Principal, Depends(guard(DataFamily.FUNDAMENTALS))],
    statement: str,
    freq: str,
    limit: Annotated[int | None, Query(ge=1)] = None,
    cursor: str | None = None,
) -> Collection[FinancialFactOut]:
    """Line items for one statement.

    `statement` and `freq` are both required, and that is a performance
    contract rather than a stylistic choice: `financial_facts` is keyed on
    (symbol, statement, freq, period_end, item_key), so without them the
    query cannot use the leading columns of its own primary key.
    """
    limits.apply_statement_timeout(session)
    code = _normalise_symbol(symbol)

    if not reads.symbol_exists(session, code):
        raise ApiProblem(404, TYPE_NOT_FOUND, "No such symbol")

    size = _page_size(request, limit)
    identity = {
        "route": "financials",
        "symbol": code,
        "statement": statement,
        "freq": freq,
        "limit": size,
    }
    after = _decode(cursor, query=identity, arity=2)

    try:
        page = reads.list_financials(
            session,
            symbol=code,
            statement_kind=statement,
            freq=freq,
            limit=size,
            after=after,
        )
    except Exception as exc:  # noqa: BLE001 - an unknown enum value is the caller's
        raise ApiProblem(
            422,
            TYPE_INVALID_PARAMETER,
            "Unknown statement or frequency",
            detail="statement and freq must match the values the pipeline stores",
        ) from exc

    as_of = reads.financials_as_of(session, code, statement, freq)
    _finish(
        response,
        request=request,
        as_of=as_of,
        settled=True,
        payload_key=f"{identity}|{cursor}",
    )
    return Collection[FinancialFactOut](
        data=[
            FinancialFactOut(
                period_end=row["period_end"],
                item_key=row["item_key"],
                value=reads.to_number(row["value"]) or "0",
                currency=row.get("currency"),
            )
            for row in page.rows
        ],
        next_cursor=(
            cursors.encode(page.next_key, query=identity) if page.next_key else None
        ),
        as_of=as_of,
    )
