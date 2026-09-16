"""The core read endpoints. Each declares `guard(family)` and nothing else
about authorisation: scope, usage counter and plan limits all come from
that one argument."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from yfin.api.auth.dependencies import Principal
from yfin.api.core.errors import (
    TYPE_INVALID_PARAMETER,
    TYPE_NOT_FOUND,
    TYPE_RANGE_TOO_LARGE,
    ApiProblem,
)
from yfin.api.core.openapi import contract
from yfin.api.ratelimit.dependencies import guard
from yfin.api.routers.v1 import paging
from yfin.api.schemas.common import Collection, Resource
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
from yfin.core import normalize as nz
from yfin.core.families import DataFamily
from yfin.models import ReadableInterval
from yfin.models.financials import StatementFreq, StatementKind

router = APIRouter(prefix="/v1", tags=["market"])

# All five read endpoints answer through `_respond`, so all five meter,
# cache and revalidate identically. Declared once here rather than five
# times: the document must not be able to describe one of them differently
# from the function they all share.
CONTRACT = contract(metered=True, cached=True, conditional=True)

SessionDep = Annotated[Session, Depends(session_scope)]

# The parameters every collection shares, described once. A caller hits
# each of these wrong exactly once, and then has to be told.
FromQuery = Annotated[
    datetime | None,
    Query(
        alias="from",
        description="Start of the range, INCLUSIVE. Half-open with `to`, so "
        "consecutive pages never overlap.",
    ),
]
ToQuery = Annotated[
    datetime | None,
    Query(
        alias="to",
        description="End of the range, EXCLUSIVE. Defaults to now.",
    ),
]
LimitQuery = Annotated[
    int | None,
    Query(
        ge=1,
        description="Rows per page. Capped by the plan; asking for more is a "
        "422 rather than a silent clip, because fewer rows than asked for "
        "is indistinguishable from reaching the end of the data.",
    ),
]
CursorQuery = Annotated[
    str | None,
    Query(
        description="The `next_cursor` of the previous page. A cursor belongs "
        "to the query that produced it: change an interval, a range or a "
        "filter and it is refused with `invalid_cursor`.",
    ),
]

#: Historic data does not change; today's does. Two values rather than a
#: formula because the difference is what matters to a client, not the
#: precise number.
CACHE_SETTLED_SECONDS = 86_400
CACHE_LIVE_SECONDS = 60


def _matches(header: str | None, etag: str) -> bool:
    """RFC 9110 §13.1.2: a comma-separated list, or `*`, compared weakly."""
    if not header:
        return False
    if header.strip() == "*":
        return True
    wanted = etag.removeprefix("W/")
    return any(tag.strip().removeprefix("W/") == wanted for tag in header.split(","))


def _respond[T: BaseModel](
    request: Request,
    response: Response,
    *,
    payload: T,
    as_of: datetime | None,
    settled: bool,
    payload_key: str,
) -> T | Response:
    """Cache headers, the freshness stamp, and the conditional answer. The
    ETag hashes the serialised body, so it changes when the data does.
    `Cache-Control: private` always: responses vary by scope and page size,
    so a shared cache would leak data between clients."""
    stamp = as_of.isoformat() if as_of else "-"
    digest = hashlib.sha256(
        f"{payload_key}|{stamp}|{payload.model_dump_json()}".encode()
    ).hexdigest()[:32]
    etag = f'W/"{digest}"'

    max_age = CACHE_SETTLED_SECONDS if settled else CACHE_LIVE_SECONDS
    response.headers["Cache-Control"] = f"private, max-age={max_age}"
    response.headers["Vary"] = "Authorization, Accept-Encoding"
    response.headers["ETag"] = etag
    if as_of is not None:
        response.headers["X-Data-As-Of"] = as_of.isoformat()

    if not _matches(request.headers.get("if-none-match"), etag):
        return payload

    # RFC 9110 §15.4.5: no content, and the headers whose value would
    # differ from the 200's. The rate headers ride along because the
    # request was metered exactly like any other -- a conditional request
    # still costs a request, and the saving is bandwidth.
    return Response(status_code=304, headers=dict(response.headers))


#: Which published error type each window refusal is. One mapping, so the
#: two endpoints cannot disagree about what a rejected range is called.
_WINDOW_TYPES = {
    limits.WindowProblem.INVERTED: TYPE_INVALID_PARAMETER,
    limits.WindowProblem.TOO_WIDE: TYPE_RANGE_TOO_LARGE,
}


def _refuse_window(problem: tuple[limits.WindowProblem, str] | None) -> None:
    if problem is None:
        return
    kind, detail = problem
    raise ApiProblem(422, _WINDOW_TYPES[kind], "Unacceptable time range", detail=detail)


# --- symbols ----------------------------------------------------------------


@router.get(
    "/symbols",
    response_model=Collection[SymbolSummary],
    summary="List symbols",
    description=(
        "Symbols in the universe, ordered by symbol.\n"
        "\n"
        "`active` defaults to true: an inactive row is one discovery found but\n"
        "an operator never activated, so the pipeline does not fetch it and it\n"
        "is close to empty."
    ),
    openapi_extra=CONTRACT,
)
def list_symbols(
    request: Request,
    response: Response,
    session: SessionDep,
    principal: Annotated[Principal, Depends(guard(DataFamily.REFERENCE))],
    exchange: Annotated[
        str | None,
        Query(max_length=limits.MAX_PARAM_LENGTH, description="Exact exchange code."),
    ] = None,
    quote_type: Annotated[
        str | None,
        Query(
            max_length=limits.MAX_PARAM_LENGTH,
            description="Exact quote type, e.g. `EQUITY` or `ETF`.",
        ),
    ] = None,
    q: Annotated[
        str | None,
        Query(
            min_length=limits.MIN_PREFIX_LENGTH,
            max_length=limits.MAX_PREFIX_LENGTH,
            description="Symbol prefix. Matches the symbol column only.",
        ),
    ] = None,
    active: Annotated[
        bool,
        Query(
            description="Only symbols an operator activated. An inactive row is "
            "one discovery found but nothing fetches, so it is close to empty.",
        ),
    ] = True,
    limit: LimitQuery = None,
    cursor: CursorQuery = None,
) -> Collection[SymbolSummary] | Response:
    limits.apply_statement_timeout(session)
    size = paging.page_size(request, limit)
    identity = {
        "route": "symbols",
        "exchange": exchange,
        "quote_type": quote_type,
        "q": q,
        "active": active,
        "limit": size,
    }
    after = paging.decode_cursor(cursor, query=identity, arity=1)

    page = reads.list_symbols(
        session,
        exchange=exchange,
        quote_type=quote_type,
        prefix=q,
        active=active,
        limit=size,
        after=after,
    )
    return _respond(
        request,
        response,
        payload=Collection[SymbolSummary](
            data=[SymbolSummary(**row) for row in page.rows],
            next_cursor=(
                cursors.encode(page.next_key, query=identity) if page.next_key else None
            ),
        ),
        as_of=None,
        settled=False,
        payload_key=f"{identity}|{cursor}",
    )


@router.get(
    "/symbols/{symbol}",
    response_model=Resource[SymbolDetail],
    summary="One symbol with its latest identity snapshot",
    openapi_extra=CONTRACT,
)
def get_symbol(
    request: Request,
    response: Response,
    session: SessionDep,
    symbol: str,
    principal: Annotated[Principal, Depends(guard(DataFamily.REFERENCE))],
) -> Resource[SymbolDetail] | Response:
    """One symbol's identity, plus the newest snapshot the pipeline holds
    for it. `info` is null for a symbol discovery found but never synced."""
    limits.apply_statement_timeout(session)
    code = nz.normalize_symbol(symbol)
    row = reads.get_symbol(session, code)
    if row is None:
        raise ApiProblem(404, TYPE_NOT_FOUND, "No such symbol")
    return _respond(
        request,
        response,
        payload=Resource[SymbolDetail](data=SymbolDetail(**row)),
        as_of=None,
        settled=False,
        # The normalised code, not the raw path parameter: otherwise
        # /v1/symbols/aapl and /v1/symbols/AAPL are two validators for one
        # representation.
        payload_key=f"symbol|{code}",
    )


# --- bars -------------------------------------------------------------------


@router.get(
    "/symbols/{symbol}/bars",
    response_model=Collection[Bar],
    summary="Price bars",
    description=(
        "Bars for one symbol, oldest first.\n"
        "\n"
        "`session` applies to intraday intervals only, and defaults to\n"
        "`regular`. That default is accident prevention: extended-hours bars\n"
        "mixed into a regular series corrupt every indicator computed from it,\n"
        "invisibly. Above daily the flag has no meaning, so passing it there is\n"
        "rejected rather than ignored."
    ),
    openapi_extra=CONTRACT,
)
def list_bars(
    request: Request,
    response: Response,
    session: SessionDep,
    symbol: str,
    principal: Annotated[Principal, Depends(guard(DataFamily.BARS))],
    interval: Annotated[
        ReadableInterval,
        Query(
            description=(
                "Bar size. Intraday intervals are routed to the intraday tables, "
                "`1d` to the daily one and `1wk`/`1mo` to the periodic one; each "
                "carries its own cap on how wide a range may be asked for."
            )
        ),
    ] = "1d",
    start: FromQuery = None,
    end: ToQuery = None,
    session_kind: Annotated[
        Literal["regular", "all"] | None,
        Query(
            alias="session",
            description="Intraday only. `regular` (the default) excludes "
            "extended-hours bars; `all` includes them. Passing it above daily "
            "is refused rather than ignored.",
        ),
    ] = None,
    limit: LimitQuery = None,
    cursor: CursorQuery = None,
) -> Collection[Bar] | Response:
    limits.apply_statement_timeout(session)
    code = nz.normalize_symbol(symbol)

    # No membership check: the annotation is the check, and it is what puts
    # the list in the document.
    is_intraday = limits.span_class(interval) == "intraday"
    if session_kind is not None and not is_intraday:
        raise ApiProblem(
            422,
            TYPE_INVALID_PARAMETER,
            "session does not apply to this interval",
            detail="the session filter is only meaningful for intraday intervals",
        )
    effective_session = (
        (session_kind or reads.SESSION_REGULAR) if is_intraday else reads.SESSION_ALL
    )

    window_start, window_end = limits.resolve_window(
        interval=interval,
        start=limits.to_utc(start),
        end=limits.to_utc(end),
        now=datetime.now(UTC),
    )
    _refuse_window(limits.window_error(interval, window_start, window_end))

    if not reads.symbol_exists(session, code):
        raise ApiProblem(404, TYPE_NOT_FOUND, "No such symbol")

    size = paging.page_size(request, limit)
    # The fingerprint covers what the CALLER sent, not the window we
    # resolved from it. An open-ended range resolves against `now`, so a
    # resolved window would differ by milliseconds between one page and
    # the next and no cursor would ever match its own query -- paging
    # without an explicit `from` would be impossible.
    identity = {
        "route": "bars",
        "symbol": code,
        "interval": interval,
        "from": start.isoformat() if start else None,
        "to": end.isoformat() if end else None,
        "session": effective_session,
        "limit": size,
    }
    after = paging.decode_cursor(cursor, query=identity, arity=1)

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

    return _respond(
        request,
        response,
        payload=Collection[Bar](
            data=[_bar(row) for row in page.rows],
            next_cursor=(
                cursors.encode(page.next_key, query=identity) if page.next_key else None
            ),
        ),
        as_of=None,
        settled=window_end
        < datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0),
        payload_key=f"{identity}|{cursor}",
    )


def _bar(row: dict[str, Any]) -> Bar:
    return Bar(
        symbol=row["symbol"],
        ts_utc=row["ts_utc"],
        bar_interval=row.get("bar_interval"),
        session_date=row.get("session_date"),
        local_date=row.get("local_date"),
        open=paging.to_number(row.get("open")),
        high=paging.to_number(row.get("high")),
        low=paging.to_number(row.get("low")),
        close=paging.to_number(row.get("close")),
        adj_close=paging.to_number(row.get("adj_close")),
        volume=row.get("volume"),
        is_extended=row.get("is_extended"),
    )


# --- corporate actions ------------------------------------------------------


@router.get(
    "/symbols/{symbol}/actions",
    response_model=Collection[Action],
    summary="Dividends, splits and capital gains",
    description=(
        "Dividends, splits and capital gains for one symbol, OLDEST FIRST.\n"
        "\n"
        "Ranges are half-open and capped like a monthly series; the action\n"
        "value's meaning depends on `action_type` -- a cash amount for a\n"
        "dividend, a ratio for a split."
    ),
    openapi_extra=CONTRACT,
)
def list_actions(
    request: Request,
    response: Response,
    session: SessionDep,
    symbol: str,
    principal: Annotated[Principal, Depends(guard(DataFamily.BARS))],
    start: FromQuery = None,
    end: ToQuery = None,
    limit: LimitQuery = None,
    cursor: CursorQuery = None,
) -> Collection[Action] | Response:
    limits.apply_statement_timeout(session)
    code = nz.normalize_symbol(symbol)

    window_start, window_end = limits.resolve_window(
        interval="1mo",
        start=limits.to_utc(start),
        end=limits.to_utc(end),
        now=datetime.now(UTC),
    )
    # The same mapping as bars.
    _refuse_window(limits.window_error("1mo", window_start, window_end))

    if not reads.symbol_exists(session, code):
        raise ApiProblem(404, TYPE_NOT_FOUND, "No such symbol")

    size = paging.page_size(request, limit)
    # As with bars: the caller's parameters, not the resolved window.
    identity = {
        "route": "actions",
        "symbol": code,
        "from": start.isoformat() if start else None,
        "to": end.isoformat() if end else None,
        "limit": size,
    }
    after = paging.decode_cursor(cursor, query=identity, arity=2)

    page = reads.list_actions(
        session,
        symbol=code,
        start=window_start,
        end=window_end,
        limit=size,
        after=after,
    )
    return _respond(
        request,
        response,
        payload=Collection[Action](
            data=[
                Action(
                    symbol=row["symbol"],
                    action_date=row["action_date"],
                    action_type=row["action_type"],
                    action_value=format(row["action_value"], "f"),
                )
                for row in page.rows
            ],
            next_cursor=(
                cursors.encode(page.next_key, query=identity) if page.next_key else None
            ),
        ),
        as_of=None,
        settled=True,
        payload_key=f"{identity}|{cursor}",
    )


# --- financials -------------------------------------------------------------


@router.get(
    "/symbols/{symbol}/financials",
    response_model=Collection[FinancialFactOut],
    summary="Financial statement line items",
    description=(
        "Line items for one statement, newest period first.\n"
        "\n"
        "`statement` and `freq` are both required, and that is a performance\n"
        "contract rather than a stylistic choice: `financial_facts` is keyed on\n"
        "(symbol, statement, freq, period_end, item_key), so without them the\n"
        "query cannot use the leading columns of its own primary key."
    ),
    openapi_extra=CONTRACT,
)
def list_financials(
    request: Request,
    response: Response,
    session: SessionDep,
    symbol: str,
    principal: Annotated[Principal, Depends(guard(DataFamily.FUNDAMENTALS))],
    statement: Annotated[
        str,
        Query(description="Which statement, e.g. `income_statement`. Required."),
    ],
    freq: Annotated[
        str, Query(description="Reporting frequency, e.g. `annual`. Required.")
    ],
    limit: LimitQuery = None,
    cursor: CursorQuery = None,
) -> Collection[FinancialFactOut] | Response:
    if statement not in StatementKind or freq not in StatementFreq:
        raise ApiProblem(
            422,
            TYPE_INVALID_PARAMETER,
            "Unknown statement or frequency",
            detail="statement and freq must match the values the pipeline stores",
        )
    limits.apply_statement_timeout(session)
    code = nz.normalize_symbol(symbol)

    if not reads.symbol_exists(session, code):
        raise ApiProblem(404, TYPE_NOT_FOUND, "No such symbol")

    size = paging.page_size(request, limit)
    identity = {
        "route": "financials",
        "symbol": code,
        "statement": statement,
        "freq": freq,
        "limit": size,
    }
    after = paging.decode_cursor(cursor, query=identity, arity=2)

    page = reads.list_financials(
        session,
        symbol=code,
        statement_kind=statement,
        freq=freq,
        limit=size,
        after=after,
    )

    as_of = reads.financials_as_of(session, code, statement, freq)
    return _respond(
        request,
        response,
        payload=Collection[FinancialFactOut](
            data=[
                FinancialFactOut(
                    period_end=row["period_end"],
                    item_key=row["item_key"],
                    value=format(row["value"], "f"),
                    currency=row.get("currency"),
                )
                for row in page.rows
            ],
            next_cursor=(
                cursors.encode(page.next_key, query=identity) if page.next_key else None
            ),
            as_of=as_of,
        ),
        as_of=as_of,
        settled=True,
        payload_key=f"{identity}|{cursor}",
    )
