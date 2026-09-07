"""The schema half of the published contract.

`core/docs.py` writes what a human reads: the introduction, the tag
descriptions, the catalogue table. This module owns what a generator
consumes -- the parts of the document that are true of many operations at
once and would otherwise be repeated in every route decorator, or, as
they were until now, in none of them.

What is here and not in the routers, and why: an endpoint added tomorrow
gets the same error responses, the same headers and the same media types
as the rest without anyone remembering to ask for them. What is NOT here
is anything true of one operation only -- a parameter description, the
`interval` enum, `OAuthError` -- because the place to state that is the
place the operation is declared.

This is enforcement, not automation. The tables below have to be kept up
to date by hand; what the design buys is that forgetting fails a test in
`tests/unit/test_api_contract.py` rather than shipping.

Two things the document said before this module existed were false. It
published `HTTPValidationError`, a body `core/errors.py` never sends,
and it published no error at all for nine of ten operations -- so a
client generated from it had no type for a 401 and no way to learn that
429 exists.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from yfin.api.core import errors
from yfin.api.core.config import ApiSettings

#: The host this API is published on. A constant rather than a setting so
#: the committed openapi.json is the same on every machine; the setting
#: below only changes what a running deployment serves.
PRODUCTION_URL = "https://yfinance.monafy.com"

JSON_MEDIA_TYPE = "application/json"

#: Operation ids, spelled the way a generated client method should read.
#: Applied to the route objects at startup, not to the finished document,
#: so `route.operation_id` and the contract cannot disagree -- and so the
#: example files, which are named after these, can be found from either.
OPERATION_IDS: dict[tuple[str, str], str] = {
    ("post", "/oauth/token"): "issueToken",
    ("get", "/health"): "getHealth",
    ("get", "/health/ready"): "getReadiness",
    ("get", "/v1/symbols"): "listSymbols",
    ("get", "/v1/symbols/{symbol}"): "getSymbol",
    ("get", "/v1/symbols/{symbol}/bars"): "listBars",
    ("get", "/v1/symbols/{symbol}/actions"): "listActions",
    ("get", "/v1/symbols/{symbol}/financials"): "listFinancials",
    ("get", "/v1/datasets"): "listDatasets",
    ("get", "/v1/datasets/{name}"): "readDataset",
}

#: The error statuses each operation publishes. Explicit per operation
#: rather than derived from a rule, because every rule anyone proposed was
#: wrong about at least one row: `listDatasets` needs no scope and touches
#: no database, so it has neither a 403 nor a 504, while 403 is raised
#: app-wide by `auth/dependencies.py` and is not a dataset speciality.
#:
#: 405 is deliberately absent. `_http_exception` turns Starlette's method
#: refusal into a problem on every path, but a caller using a method the
#: document does not list has already left the contract; describing it in
#: the introduction beats ten identical response objects.
ERROR_STATUSES: dict[str, tuple[int, ...]] = {
    "issueToken": (400, 401, 422, 429, 503),
    "getHealth": (500,),
    "getReadiness": (429, 500),
    "listSymbols": (401, 403, 422, 429, 500, 504),
    "getSymbol": (401, 403, 404, 422, 429, 500, 504),
    "listBars": (401, 403, 404, 422, 429, 500, 504),
    "listActions": (401, 403, 404, 422, 429, 500, 504),
    "listFinancials": (401, 403, 404, 422, 429, 500, 504),
    "listDatasets": (401, 422, 429, 500),
    "readDataset": (401, 403, 404, 422, 429, 500, 504),
}

#: Operations that meter the request, and therefore carry the rate and
#: quota headers. Not the same as "has security": `listDatasets` requires
#: a token but no scope, and still meters.
METERED = frozenset(
    {
        "listSymbols",
        "getSymbol",
        "listBars",
        "listActions",
        "listFinancials",
        "readDataset",
    }
)

#: Operations under `/v1`, which set cache headers on success.
CACHED = frozenset(METERED | {"listDatasets"})

#: Operations that answer `If-None-Match` with a 304. Exactly the handlers
#: that go through `market._respond`; an explicit set because the document
#: builder cannot see a call graph. The dataset routes are absent on
#: purpose -- they emit no validator, so there is nothing to revalidate
#: against, and adding one is a change to that surface, not to this one.
CONDITIONAL = frozenset(
    {"listSymbols", "getSymbol", "listBars", "listActions", "listFinancials"}
)

#: The one operation whose errors are RFC 6749, not RFC 9457.
TOKEN_OPERATION = "issueToken"

#: Status -> (schema name, the problem types it may carry). A single enum
#: over all thirteen types on every response would publish that a 404 may
#: answer `quota_exceeded`; narrowing per status is what makes the
#: document say what the code does. Every type in `errors.ALL_TYPES` has
#: a home here, and a test asserts it.
PROBLEM_VARIANTS: dict[int, tuple[str, tuple[str, ...]]] = {
    401: (
        "Unauthenticated",
        (errors.TYPE_UNAUTHENTICATED, errors.TYPE_INVALID_TOKEN, errors.TYPE_CLIENT_DISABLED),
    ),
    403: ("Forbidden", (errors.TYPE_INSUFFICIENT_SCOPE,)),
    404: ("NotFound", (errors.TYPE_NOT_FOUND,)),
    422: ("InvalidRequest", (errors.TYPE_INVALID_PARAMETER, errors.TYPE_INVALID_CURSOR)),
    429: (
        "RateLimited",
        (errors.TYPE_RATE_LIMIT, errors.TYPE_QUOTA, errors.TYPE_CONCURRENCY),
    ),
    500: ("InternalError", (errors.TYPE_INTERNAL,)),
    504: ("QueryTimeout", (errors.TYPE_QUERY_TIMEOUT,)),
}

#: One operation refuses a range that is merely too wide, which is not the
#: same failure as a malformed parameter and is worth its own type. It is
#: the only variant that is per operation rather than per status.
BARS_VARIANT = (
    "InvalidBarsRequest",
    (*PROBLEM_VARIANTS[422][1], errors.TYPE_RANGE_TOO_LARGE),
)

STATUS_TITLES = {
    304: "The representation has not changed since the ETag you sent",
    400: "The request is malformed",
    401: "Authentication failed or the token is not usable",
    403: "The token does not carry the required scope",
    404: "No such resource",
    422: "The request parameters are not acceptable",
    429: "A rate limit, concurrency limit or the monthly quota was hit",
    500: "Internal server error",
    503: "Authentication is temporarily unavailable",
    504: "The query took too long and was cancelled",
}


# --- headers ----------------------------------------------------------------


def _header(description: str, schema: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"description": description, "schema": schema, **extra}


_COUNT = {"type": "integer", "minimum": 0}
_TEXT = {"type": "string"}

#: On every response, from `SecurityHeadersMiddleware` and
#: `RequestContextMiddleware`. Published because a caller debugging a
#: failure needs to know the id exists and what to quote.
UNIVERSAL_HEADERS = {
    "X-Request-Id": _header(
        "Identifies this request in the server's logs. Quote it in a support request.",
        _TEXT,
    ),
    "Strict-Transport-Security": _header("Fixed policy.", _TEXT),
    "X-Content-Type-Options": _header("Always `nosniff`.", _TEXT),
    "Referrer-Policy": _header("Always `no-referrer`.", _TEXT),
}

#: The two families are separate because they answer different questions
#: and reset on different clocks -- see `ratelimit/limiter.py:headers`.
#: Both resets are in SECONDS, not timestamps, which is the single thing
#: a client most often gets wrong here.
RATE_HEADERS = {
    "RateLimit-Limit": _header("Requests allowed per second by the plan.", _COUNT),
    "RateLimit-Remaining": _header("Requests left in the current second.", _COUNT),
    "RateLimit-Reset": _header("Seconds until the per-second budget refills.", _COUNT),
    "X-Quota-Limit": _header("Requests allowed this month by the plan.", _COUNT),
    "X-Quota-Remaining": _header("Requests left this month.", _COUNT),
    "X-Quota-Reset": _header("Seconds until the monthly quota resets.", _COUNT),
}

#: Only where a validator is actually emitted. `X-Data-As-Of` is absent
#: wherever the schema records no fetch time -- the price tables
#: deliberately keep none, since a per-row timestamp would cost gigabytes
#: to answer a question the bar's own ts_utc already covers.
VALIDATOR_HEADERS = {
    "ETag": _header(
        "Weak validator, `W/\"...\"`. Derived from the response body, so it "
        "changes when the data does. Send it back as `If-None-Match`.",
        _TEXT,
    ),
    "X-Data-As-Of": _header(
        "When this data was last verified against the source. Absent where the "
        "schema keeps no fetch timestamp.",
        {"type": "string", "format": "date-time"},
        required=False,
    ),
}

CACHE_HEADERS = {
    "Cache-Control": _header(
        "Always `private`: responses vary by scope and by the plan's page size, "
        "so a shared cache must not hold one and serve it to another client.",
        _TEXT,
    ),
    "Vary": _header("Always includes `Authorization`.", _TEXT),
}

RETRY_HEADER = {
    "Retry-After": _header(
        "Seconds to wait before retrying. Never an HTTP-date.", _COUNT
    )
}

TOKEN_HEADERS = {
    "Cache-Control": _header("Always `no-store` (RFC 6749 §5.1).", _TEXT),
    "Pragma": _header("Always `no-cache`.", _TEXT),
}

_WWW_AUTHENTICATE = {
    401: _header(
        "The challenge, per RFC 6750 §3.",
        _TEXT,
        example='Bearer realm="yfin-api", error="invalid_token"',
    ),
    403: _header(
        "Names the scope the token is missing, per RFC 6750 §3.",
        _TEXT,
        example='Bearer error="insufficient_scope", scope="bars:read"',
    ),
}

_TOKEN_WWW_AUTHENTICATE = _header(
    "The Basic challenge, per RFC 6749 §5.2.",
    _TEXT,
    example='Basic realm="yfin-api", charset="UTF-8"',
)


def _headers_for(operation_id: str, status: int) -> dict[str, Any]:
    """Exactly the headers this response carries, and no others.

    The distinctions are not cosmetic. `problem_response` builds a fresh
    response with only the headers the raiser attached, so a 404 or a 504
    genuinely has no rate headers on it, while a 429 does -- the limiter
    merges them in deliberately. Publishing them everywhere would be
    easier and would be a lie a client could act on.
    """
    headers: dict[str, Any] = dict(UNIVERSAL_HEADERS)

    if operation_id == TOKEN_OPERATION:
        headers.update(TOKEN_HEADERS)
        if status in (401, 400):
            headers["WWW-Authenticate"] = _TOKEN_WWW_AUTHENTICATE
        if status in (429, 503):
            headers.update(RETRY_HEADER)
        return headers

    if operation_id in METERED and status in (200, 304, 429):
        headers.update(RATE_HEADERS)
    if operation_id in CACHED and status in (200, 304):
        headers.update(CACHE_HEADERS)
    if operation_id in CONDITIONAL and status in (200, 304):
        # RFC 9110 §15.4.5: a 304 carries the headers whose value would
        # differ from the 200's, the validator above all.
        headers.update(VALIDATOR_HEADERS)
    if status == 429:
        headers.update(RETRY_HEADER)
    if status in _WWW_AUTHENTICATE:
        headers["WWW-Authenticate"] = _WWW_AUTHENTICATE[status]
    return headers


# --- the document -----------------------------------------------------------


def servers_for(settings: ApiSettings) -> list[dict[str, str]]:
    """Where the API is, for a generator that has only the document.

    The relative entry is second and is not decoration: it is what lets
    `/docs` on a laptop or a staging host call itself instead of calling
    production. `tokenUrl` stays relative for the same reason and is
    unaffected by this list -- in OpenAPI 3.1 it resolves against the
    document's own URL, not against `servers`.
    """
    base = settings.public_base_url.strip().rstrip("/") or PRODUCTION_URL
    return [
        {"url": base, "description": "Production"},
        {"url": "/", "description": "This deployment"},
    ]


def problem_schemas() -> dict[str, Any]:
    """`Problem` and its per-status narrowings."""
    problem = {
        "type": "object",
        "title": "Problem",
        "description": (
            "RFC 9457 problem detail. `type` is an OPAQUE TOKEN, not a URL: do "
            "not dereference it, branch on it. The occurrence is identified by "
            "`request_id`, which is also the `X-Request-Id` of the response."
        ),
        "required": ["type", "title", "status"],
        "properties": {
            "type": {
                "type": "string",
                "enum": list(errors.ALL_TYPES),
                "description": "What was refused. Stable; safe to branch on.",
            },
            "title": {"type": "string", "description": "A short, human-readable summary."},
            "status": {
                "type": "integer",
                "description": "The HTTP status, repeated in the body.",
            },
            "detail": {
                "type": "string",
                "description": "What to change about the request, when there is something.",
            },
            "request_id": {
                "type": ["string", "null"],
                "description": "Identifies this request in the server's logs.",
            },
        },
    }

    schemas: dict[str, Any] = {"Problem": problem}
    variants = [*PROBLEM_VARIANTS.values(), BARS_VARIANT]
    for name, types in variants:
        schemas[name] = {
            "allOf": [
                {"$ref": "#/components/schemas/Problem"},
                {"properties": {"type": {"enum": list(types)}}},
            ]
        }
    return schemas


def _variant_ref(operation_id: str, status: int) -> str:
    if operation_id == "listBars" and status == 422:
        name = BARS_VARIANT[0]
    else:
        name = PROBLEM_VARIANTS[status][0]
    return f"#/components/schemas/{name}"


def _problem_response(operation_id: str, status: int) -> dict[str, Any]:
    return {
        "description": STATUS_TITLES[status],
        "content": {
            errors.PROBLEM_MEDIA_TYPE: {"schema": {"$ref": _variant_ref(operation_id, status)}}
        },
        "headers": _headers_for(operation_id, status),
    }


def install(app: FastAPI) -> None:
    """Names the routes and replaces the document builder.

    Memoised into `app.openapi_schema` the way FastAPI's own
    implementation is, and installed whether or not the docs are served:
    a deployment that withholds `/openapi.json` must still BE the
    application the committed contract describes.
    """
    _name_routes(app)

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = finalise(_generate(app))
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]


def walk_routes(routes: Iterable[Any]) -> Iterator[Any]:
    """Every route, through the wrappers `include_router` leaves behind.

    `app.routes` is not flat: FastAPI wraps an included router in a node
    whose own `routes` hold the real ones, and it has no `path`. Iterating
    the top level only -- which is what this did first -- silently found
    nothing but the four documentation routes, named none of the API's
    operations, and left the generated ids in the contract with no error
    anywhere to say so.
    """
    for route in routes:
        # `_IncludedRouter` is a matcher, not a route: it holds the real
        # ones on `original_router` and exposes neither `path` nor
        # `routes` itself.
        included = getattr(route, "original_router", None)
        nested = getattr(included, "routes", None) or getattr(route, "routes", None)
        if nested:
            yield from walk_routes(nested)
        if getattr(route, "path", None) is not None:
            yield route


def _name_routes(app: FastAPI) -> None:
    for route in walk_routes(app.routes):
        for method in getattr(route, "methods", None) or ():
            operation_id = OPERATION_IDS.get((method.lower(), route.path))
            if operation_id is not None:
                route.operation_id = operation_id


def _generate(app: FastAPI) -> dict[str, Any]:
    return get_openapi(
        title=app.title,
        version=app.version,
        summary=app.summary,
        description=app.description,
        routes=app.routes,
        tags=app.openapi_tags,
        servers=app.servers,
        contact=app.contact,
        license_info=app.license_info,
    )


def finalise(document: dict[str, Any]) -> dict[str, Any]:
    """Everything the generated document does not know about itself."""
    schemas = document.setdefault("components", {}).setdefault("schemas", {})
    schemas.update(problem_schemas())

    for path, operations in document["paths"].items():
        for method, operation in operations.items():
            operation_id = OPERATION_IDS.get((method.lower(), path))
            if operation_id is None:
                # A route not in the map is a route nobody named. The
                # contract test refuses it; leaving it untouched here
                # keeps this function free of a second opinion.
                continue
            _apply(operation, operation_id)

    # Nothing references them once every 422 is either a problem variant
    # or `OAuthError`. They are removed rather than left as dead weight
    # because a generated client would still emit types for them, and a
    # reader would reasonably conclude the API can send one.
    for dead in ("HTTPValidationError", "ValidationError"):
        schemas.pop(dead, None)

    return document


def _apply(operation: dict[str, Any], operation_id: str) -> None:
    responses = operation.setdefault("responses", {})

    if operation_id in CONDITIONAL:
        # No `content`: RFC 9110 forbids a body here, and a response object
        # with only a description is the legal way to say so.
        responses["304"] = {"description": STATUS_TITLES[304]}

    for status in ERROR_STATUSES[operation_id]:
        key = str(status)
        if operation_id == TOKEN_OPERATION:
            # Declared on the route with `OAuthError`, so the body is
            # already right; only the headers are missing.
            responses.setdefault(key, {"description": STATUS_TITLES[status]})
        else:
            responses[key] = _problem_response(operation_id, status)

    for status_key, response in responses.items():
        if not status_key.isdigit():
            continue
        headers = _headers_for(operation_id, int(status_key))
        if headers:
            response.setdefault("headers", {}).update(headers)
