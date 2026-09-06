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
from starlette.exceptions import HTTPException as StarletteHTTPException

from yfin.core.logging_setup import get_logger

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
TYPE_DEPENDENCY = "dependency_unavailable"
TYPE_QUERY_TIMEOUT = "query_timeout"
TYPE_INTERNAL = "internal_error"


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


def problem_response(
    request: Request,
    status: int,
    problem_type: str,
    title: str,
    *,
    detail: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
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
    app.add_exception_handler(Exception, _unhandled)
