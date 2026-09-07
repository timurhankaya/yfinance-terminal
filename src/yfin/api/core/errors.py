"""RFC 9457 problem details, and the handlers that keep internals out.

One rule governs every body here: a client learns *what* was refused,
never *how* the server is built. No exception text, no SQL, no table or
column name, no file path, no stack trace -- those go to the log, keyed
by the same request_id the client is handed.

`/oauth/token` is the one endpoint that does NOT use this format; RFC
6749 §5.2 requires its own error body, and standard OAuth2 clients parse
that shape. See `api/routers/oauth.py`.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from yfin.core.logging_setup import get_logger
from yfin.core.metrics import inc

log = get_logger(__name__)

PROBLEM_MEDIA_TYPE = "application/problem+json"

# `type` values are part of the published contract: clients branch on
# them, so they are stable strings rather than generated URLs.
TYPE_UNAUTHENTICATED = "unauthenticated"
TYPE_INVALID_TOKEN = "invalid_token"
TYPE_CLIENT_DISABLED = "client_disabled"
TYPE_INSUFFICIENT_SCOPE = "insufficient_scope"
TYPE_NOT_FOUND = "not_found"
TYPE_INVALID_PARAMETER = "invalid_parameter"
TYPE_INVALID_CURSOR = "invalid_cursor"
TYPE_RANGE_TOO_LARGE = "range_too_large"
TYPE_RATE_LIMIT = "rate_limit_exceeded"
TYPE_QUOTA = "quota_exceeded"
TYPE_CONCURRENCY = "concurrency_limit"
TYPE_QUERY_TIMEOUT = "query_timeout"
TYPE_INTERNAL = "internal_error"

#: Every type the API can emit, as an explicit tuple rather than a scan of
#: this module's globals. The published document enumerates these, and a
#: reflective version would absorb any future name beginning `TYPE_` --
#: including one that is not an error type -- into the contract without
#: anyone deciding to. A test asserts this covers the constants above.
ALL_TYPES: tuple[str, ...] = (
    TYPE_UNAUTHENTICATED,
    TYPE_INVALID_TOKEN,
    TYPE_CLIENT_DISABLED,
    TYPE_INSUFFICIENT_SCOPE,
    TYPE_NOT_FOUND,
    TYPE_INVALID_PARAMETER,
    TYPE_INVALID_CURSOR,
    TYPE_RANGE_TOO_LARGE,
    TYPE_RATE_LIMIT,
    TYPE_QUOTA,
    TYPE_CONCURRENCY,
    TYPE_QUERY_TIMEOUT,
    TYPE_INTERNAL,
)

#: The one path whose errors are not problem documents. Kept here rather
#: than imported from the router: this module decides the format, and
#: importing the router would make the error layer depend on the routing
#: layer it exists to serve.
TOKEN_ENDPOINT_PATH = "/oauth/token"

#: Which RFC 6749 error each status carries when a generic handler, not
#: the token endpoint's own code, is what refused the request.
_OAUTH_ERRORS = {
    422: ("invalid_request", "the request is missing a required parameter"),
    500: ("server_error", "the authorisation server encountered an unexpected condition"),
    504: ("temporarily_unavailable", "the authorisation server is overloaded"),
}


class ApiProblem(Exception):
    """An error that is safe to show a client, verbatim.

    Anything raised as an ApiProblem has been phrased for the outside
    world; anything else becomes a bare 500 (see `_unhandled`).
    """

    def __init__(
        self,
        status: int,
        problem_type: str,
        title: str,
        *,
        detail: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(title)
        self.status = status
        self.problem_type = problem_type
        self.title = title
        self.detail = detail
        self.headers = headers or {}


def _oauth_shaped(
    status: int,
    title: str,
    *,
    detail: str | None,
    headers: dict[str, str] | None,
) -> JSONResponse:
    """RFC 6749 §5.2 body for a failure the token endpoint did not phrase.

    Two members only, exactly as `_oauth_error` in the router builds them,
    so every failure of this endpoint looks the same to a client library
    whether the router refused it or a generic handler did. The request id
    is still on the response, in `X-Request-Id`.
    """
    error, description = _OAUTH_ERRORS.get(status, ("invalid_request", title))
    all_headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    all_headers.update(headers or {})
    return JSONResponse(
        {"error": error, "error_description": detail or description},
        status_code=status,
        headers=all_headers,
    )


def problem_response(
    request: Request,
    status: int,
    problem_type: str,
    title: str,
    *,
    detail: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    # Before the OAuth branch, not after: the label set is `ALL_TYPES`, and
    # an error that left through the token endpoint's own shape is still an
    # error the dashboard has to see. This is the one function every refusal
    # passes through, which is why the counter is here and not at each of
    # the two dozen `raise ApiProblem` sites.
    inc("yfin_api_problems_total", type=problem_type)

    if request.url.path == TOKEN_ENDPOINT_PATH:
        # The token endpoint's own code answers in the RFC 6749 shape, but
        # the generic handlers registered below do not know that. Without
        # this, a missing `grant_type` (RequestValidationError), an
        # unhandled failure or a cancelled query would reach an OAuth2
        # client library as a problem document with no `error` field --
        # exactly the failure `routers/oauth.py` documents at length and
        # which no schema test would catch, because the document would
        # still match what we published.
        return _oauth_shaped(status, title, detail=detail, headers=headers)

    body: dict[str, Any] = {
        "type": problem_type,
        "title": title,
        "status": status,
        "request_id": getattr(request.state, "request_id", None),
    }
    if detail is not None:
        body["detail"] = detail
    return JSONResponse(
        body, status_code=status, media_type=PROBLEM_MEDIA_TYPE, headers=headers
    )


async def _api_problem(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiProblem)
    return problem_response(
        request,
        exc.status,
        exc.problem_type,
        exc.title,
        detail=exc.detail,
        headers=exc.headers,
    )


async def _http_exception(request: Request, exc: Exception) -> JSONResponse:
    """Starlette's own 404/405 and anything raised as HTTPException."""
    assert isinstance(exc, StarletteHTTPException)
    problem_type = TYPE_NOT_FOUND if exc.status_code == 404 else TYPE_INVALID_PARAMETER
    headers = dict(exc.headers or {})
    return problem_response(
        request, exc.status_code, problem_type, str(exc.detail), headers=headers
    )


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    """422 without echoing the submitted value.

    FastAPI's default body carries `input`, i.e. whatever the client
    sent, and the internal field path. Both are reflected straight back;
    for a public API that is a needless amplification surface and leaks
    the shape of our models.
    """
    assert isinstance(exc, RequestValidationError)
    fields = sorted(
        {".".join(str(p) for p in err.get("loc", ())[1:]) or "body" for err in exc.errors()}
    )
    return problem_response(
        request,
        422,
        TYPE_INVALID_PARAMETER,
        "Request parameters failed validation",
        detail=f"invalid: {', '.join(fields)}",
    )


#: PostgreSQL's SQLSTATE for a statement cancelled by statement_timeout.
QUERY_CANCELED = "57014"


async def _operational_error(request: Request, exc: Exception) -> JSONResponse:
    """A cancelled query is the caller's answer, not an internal failure.

    Without this the statement timeout in `storage/limits.py` surfaces as
    a 500, which is wrong twice over: the caller learns nothing about what
    to change, and a 500 refunds the request's quota unit -- so asking for
    something too expensive to serve would cost nothing, which is an
    invitation to keep asking.
    """
    assert isinstance(exc, OperationalError)
    if getattr(exc.orig, "sqlstate", None) == QUERY_CANCELED:
        return problem_response(
            request,
            504,
            TYPE_QUERY_TIMEOUT,
            "The query took too long and was cancelled",
            detail="narrow the range or the page size and try again",
        )
    return await _unhandled(request, exc)


async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    """Anything we did not phrase ourselves becomes an opaque 500."""
    route = request.scope.get("route")
    log.exception(
        "unhandled_error",
        request_id=getattr(request.state, "request_id", None),
        # The route template, not the URL: a query string can carry a
        # secret a client put there by mistake.
        route=getattr(route, "path", request.url.path),
    )
    return problem_response(request, 500, TYPE_INTERNAL, "Internal server error")


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiProblem, _api_problem)
    app.add_exception_handler(StarletteHTTPException, _http_exception)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(OperationalError, _operational_error)
    app.add_exception_handler(Exception, _unhandled)
