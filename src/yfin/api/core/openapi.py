"""The schema half of the published contract: what is true of many
operations at once (error responses, headers, media types), as opposed to
`core/docs.py` prose. Anything true of one operation only is declared at
the route. The tables here are hand-kept; forgetting one fails
`tests/unit/test_api_contract.py`."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from yfin.api.core import errors
from yfin.api.core.config import ApiSettings

#: The host this API is published on. A constant rather than a setting so
#: the committed openapi.json is the same on every machine; the setting
#: below only changes what a running deployment serves.
PRODUCTION_URL = "https://yfinance.monafy.com"

#: The icon both documentation pages use, inline: FastAPI's default is
#: fetched from a third-party host on every read of the contract.
FAVICON = (
    "data:image/svg+xml,"
    "%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%2016%2016'%3E"
    "%3Crect%20width='16'%20height='16'%20rx='3'%20fill='%23111'/%3E"
    "%3Cpath%20d='M3%2012L6%208L9%2010L13%204'%20stroke='%236ee7b7'%20"
    "stroke-width='1.8'%20fill='none'%20stroke-linecap='round'%20"
    "stroke-linejoin='round'/%3E%3C/svg%3E"
)

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

#: The error statuses each operation publishes, explicit per operation:
#: no rule fits every row (`listDatasets` needs no scope and no database,
#: so neither 403 nor 504). 405 is left to the introduction; a caller
#: using an unlisted method has already left the contract.
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

#: The key the route decorator's declaration travels under. It is read out
#: of the generated operation and REMOVED before the document is published:
#: what it says is true of the handler, not of the contract.
CONTRACT_KEY = "x-yfin-contract"


@dataclass(frozen=True)
class RouteContract:
    """What a handler does that the document cannot see by reading it,
    declared at the route. It cannot be derived from `guard()`: the dataset
    routes meter from inside the handler. `tests/repo/test_api_headers.py`
    compares the headers actually sent against what is published here."""

    #: Meters the request, and therefore carries the rate and quota
    #: headers. Not the same as "has security": `listDatasets` needs a
    #: token but no scope, and still meters, under the `meta` family.
    metered: bool = False
    #: Sets cache headers on success.
    cached: bool = False
    #: Answers `If-None-Match` with a 304 and emits a validator. The
    #: dataset routes are deliberately not conditional -- they emit no
    #: validator, so there is nothing to revalidate against.
    conditional: bool = False


def contract(
    *, metered: bool = False, cached: bool = False, conditional: bool = False
) -> dict[str, Any]:
    """The declaration, for a route decorator's `openapi_extra`."""
    return {
        CONTRACT_KEY: {"metered": metered, "cached": cached, "conditional": conditional}
    }

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

#: Some operations refuse a range that is merely too wide, which is not
#: the same failure as a malformed parameter and is worth its own type.
#: The only variant that is per operation rather than per status.
RANGE_VARIANT = (
    "InvalidRangeRequest",
    (*PROBLEM_VARIANTS[422][1], errors.TYPE_RANGE_TOO_LARGE),
)

#: The operations that can answer with `range_too_large`. `listActions`
#: was missing and sent a type its own published schema forbade.
RANGED = frozenset({"listBars", "listActions"})

#: The examples the document must carry, as
#: `operationId -> {status -> (example name, ...)}`: one of each shape plus
#: the likely failures. Each entry is a file under `examples/`, captured
#: from a real response by `tests/repo/test_api_examples.py`.
REQUIRED_EXAMPLES: dict[str, dict[int, tuple[str, ...]]] = {
    "issueToken": {200: ("token",), 400: ("invalid_request",), 401: ("invalid_client",)},
    "getHealth": {200: ("up",)},
    "listSymbols": {200: ("page",), 401: ("unauthenticated",), 429: ("rate_limit_exceeded",)},
    "getSymbol": {200: ("symbol",), 404: ("not_found",)},
    "listBars": {200: ("page",), 422: ("range_too_large",)},
    "listActions": {200: ("page",)},
    "listFinancials": {200: ("page",)},
    "listDatasets": {200: ("catalogue",)},
    "readDataset": {
        200: ("page",),
        403: ("insufficient_scope",),
        404: ("not_found",),
        422: ("invalid_cursor", "invalid_parameter"),
    },
}

#: An example value for every parameter, keyed by the name a caller sends
#: so a name shared by several operations is illustrated the same way.
#: `ACME` is fictional so no illustrative number reads as a real figure.
PARAMETER_EXAMPLES: dict[str, Any] = {
    "symbol": "ACME",
    "name": "major_holders",
    "interval": "1d",
    "from": "2026-01-01T00:00:00Z",
    "to": "2026-02-01T00:00:00Z",
    "session": "regular",
    "limit": 100,
    "cursor": "eyJrIjpbIjIwMjYtMDEtMDUiXSwicSI6IjhmMmEifQ",
    "q": "AC",
    "exchange": "NMS",
    "quote_type": "EQUITY",
    "active": True,
    "all": False,
    "statement": "income",
    "freq": "annual",
}

#: The form body, which has no captured example: a real 200 needs a client
#: row with a hashed secret, and what a reader needs here is the shape of
#: the REQUEST anyway.
TOKEN_REQUEST_EXAMPLE = {
    "grant_type": "client_credentials",
    "scope": "reference:read bars:read",
}

#: Where those files live. Inside the package, not under `docs/`: they are
#: part of the document the API serves, so an installation from a wheel
#: must carry them or production would publish a different contract from
#: the one this repository locks.
EXAMPLES_DIR = Path(__file__).resolve().parent / "examples"


def example_path(operation_id: str, status: int, name: str) -> Path:
    return EXAMPLES_DIR / f"{operation_id}.{status}.{name}.json"


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


def _headers_for(operation_id: str, declared: RouteContract, status: int) -> dict[str, Any]:
    """Exactly the headers this response carries: `problem_response` builds a
    fresh response with only what the raiser attached, so a 404 has no rate
    headers while a 429 does."""
    headers: dict[str, Any] = dict(UNIVERSAL_HEADERS)

    if operation_id == TOKEN_OPERATION:
        headers.update(TOKEN_HEADERS)
        if status in (401, 400):
            headers["WWW-Authenticate"] = _TOKEN_WWW_AUTHENTICATE
        if status in (429, 503):
            headers.update(RETRY_HEADER)
        return headers

    if declared.metered and status in (200, 304, 429):
        headers.update(RATE_HEADERS)
    if declared.cached and status in (200, 304):
        headers.update(CACHE_HEADERS)
    if declared.conditional and status in (200, 304):
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
    """The `servers` list. The relative entry lets `/docs` on a laptop or
    staging host call itself instead of production; `tokenUrl` resolves
    against the document's own URL, not this list."""
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
    variants = [*PROBLEM_VARIANTS.values(), RANGE_VARIANT]
    for name, types in variants:
        schemas[name] = {
            "allOf": [
                {"$ref": "#/components/schemas/Problem"},
                {"properties": {"type": {"enum": list(types)}}},
            ]
        }
    return schemas


def _variant_ref(operation_id: str, status: int) -> str:
    if operation_id in RANGED and status == 422:
        name = RANGE_VARIANT[0]
    else:
        name = PROBLEM_VARIANTS[status][0]
    return f"#/components/schemas/{name}"


def _problem_response(
    operation_id: str, declared: RouteContract, status: int
) -> dict[str, Any]:
    return {
        "description": STATUS_TITLES[status],
        "content": {
            errors.PROBLEM_MEDIA_TYPE: {"schema": {"$ref": _variant_ref(operation_id, status)}}
        },
        "headers": _headers_for(operation_id, declared, status),
    }


def install(app: FastAPI) -> None:
    """Names the routes and replaces the document builder. Installed whether
    or not the docs are served: a deployment that withholds `/openapi.json`
    must still be the application the committed contract describes."""
    _name_routes(app)

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = finalise(_generate(app))
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]


def walk_routes(routes: Iterable[Any]) -> Iterator[Any]:
    """Every route, through the wrappers `include_router` leaves behind:
    `app.routes` is not flat, and the top level alone holds only the
    documentation routes."""
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


def _declared_contract(operation: dict[str, Any], operation_id: str) -> RouteContract:
    """Reads the route's declaration and takes it back out of the document.
    Missing is an error, not a default: a silent `RouteContract()` would
    publish no rate headers for a route that meters."""
    declared = operation.pop(CONTRACT_KEY, None)
    if declared is None:
        raise ValueError(
            f"{operation_id} declares no `openapi_extra=contract(...)`; the "
            "document cannot tell whether it meters, caches or revalidates"
        )
    return RouteContract(**declared)


def _apply(operation: dict[str, Any], operation_id: str) -> None:
    declared = _declared_contract(operation, operation_id)
    responses = operation.setdefault("responses", {})

    if declared.conditional:
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
            responses[key] = _problem_response(operation_id, declared, status)

    for status_key, response in responses.items():
        if not status_key.isdigit():
            continue
        headers = _headers_for(operation_id, declared, int(status_key))
        if headers:
            response.setdefault("headers", {}).update(headers)

    _attach_examples(responses, operation_id)
    _attach_parameter_examples(operation)
    if operation_id == TOKEN_OPERATION:
        _attach_request_example(operation)


def _attach_parameter_examples(operation: dict[str, Any]) -> None:
    """A value a reader can paste, on every parameter. On the Parameter
    Object, not inside its schema: Swagger UI pre-fills "Try it out" from
    the former only."""
    for parameter in operation.get("parameters", ()):
        example = PARAMETER_EXAMPLES.get(parameter["name"])
        if example is not None:
            parameter["example"] = example


def _attach_request_example(operation: dict[str, Any]) -> None:
    body = operation.get("requestBody")
    if body is None:
        return
    for media in body["content"].values():
        media["examples"] = {"client_credentials": {"value": TOKEN_REQUEST_EXAMPLE}}


def _attach_examples(responses: dict[str, Any], operation_id: str) -> None:
    """Real responses, captured and committed. Plural `examples`: OpenAPI 3.1
    deprecates `example` on a Media Type Object and one status may admit
    several bodies. A missing file is skipped, not raised: that would take
    `/docs` down over a documentation file, and only the database CI job
    can produce one. `tests/unit/test_api_contract.py` checks presence."""
    for status, names in REQUIRED_EXAMPLES.get(operation_id, {}).items():
        response = responses.get(str(status))
        if response is None or "content" not in response:
            continue
        for media in response["content"].values():
            examples = media.setdefault("examples", {})
            for name in names:
                path = example_path(operation_id, status, name)
                if path.is_file():
                    examples[name] = {"value": json.loads(path.read_text("utf-8"))}
            if not examples:
                del media["examples"]
