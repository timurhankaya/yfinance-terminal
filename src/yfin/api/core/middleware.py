"""Request context, client IP resolution and security headers.

`X-Forwarded-For` is honoured only when the connection comes from a
configured proxy network, walking the chain from the right past trusted
hops; otherwise the header is attacker-writable."""

from __future__ import annotations

import ipaddress
import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import HTTPConnection, Request
from starlette.responses import Response

from yfin.api.core.config import ApiSettings
from yfin.core.logging_setup import get_logger
from yfin.core.text import comma_list

log = get_logger(__name__)

SECURITY_HEADERS = {
    # TLS is terminated at the reverse proxy; the header is what stops a
    # client from ever trying plain HTTP again. Secrets ride in the Basic
    # header on /oauth/token, so a single http:// client is a lasting leak.
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

_UNKNOWN_IP = "unknown"


def trusted_networks(settings: ApiSettings) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    nets = []
    for cidr in settings.trusted_proxy_list():
        try:
            nets.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            log.error("invalid_trusted_proxy_cidr", cidr=cidr)
    return nets


def _is_trusted(
    address: str, nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network]
) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(parsed in net for net in nets)


def resolve_client_ip(
    request: HTTPConnection, nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network]
) -> str:
    """The address rate limits and logs are keyed on. No trusted networks
    means the forwarded header is ignored outright. Typed as `HTTPConnection`
    because `/ui/ws` keys its connection limit on the same address."""
    peer = request.client.host if request.client else _UNKNOWN_IP
    if not nets or not _is_trusted(peer, nets):
        return peer

    forwarded = request.headers.get("x-forwarded-for", "")
    hops = comma_list(forwarded)
    # Walk right-to-left past every hop we trust; the first address that
    # is not one of our proxies is the client. Anything further left was
    # written by someone we do not control.
    for hop in reversed(hops):
        if not _is_trusted(hop, nets):
            return hop
    return peer


class SettingsMiddleware(BaseHTTPMiddleware):
    """A `BaseHTTPMiddleware` constructed with `ApiSettings`. Subclasses
    resolve their configuration in `__init__` so nothing reads settings per
    request."""

    def __init__(self, app: Callable[..., object], settings: ApiSettings) -> None:
        # `app` is deliberately wider than Starlette's `ASGIApp` alias:
        # the middleware stack passes an already-wrapped callable, and
        # narrowing it here would be a lie that only mypy believes.
        super().__init__(app)  # type: ignore[arg-type]
        self.settings = settings


class RequestContextMiddleware(SettingsMiddleware):
    """Assigns a request id, binds log context, times the request. The log
    line carries the route template, never the query string, which may
    hold a client's secret."""

    def __init__(self, app: Callable[..., object], settings: ApiSettings) -> None:
        super().__init__(app, settings)
        self._nets = trusted_networks(settings)

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = uuid.uuid4().hex
        request.state.request_id = request_id
        request.state.client_ip = resolve_client_ip(request, self._nets)

        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")

        route = request.scope.get("route")
        if _is_noise(request.url.path):
            response.headers["X-Request-Id"] = request_id
            return response

        log.debug(
            "request",
            request_id=request_id,
            method=request.method,
            route=getattr(route, "path", request.url.path),
            status=response.status_code,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            client_ip=request.state.client_ip,
        )
        response.headers["X-Request-Id"] = request_id
        return response


#: Paths whose requests are not logged: scrape and probe traffic would
#: drown the lines that say something. The request id header is still set
#: and a failure is still visible as a metric.
_UNLOGGED = ("/metrics", "/health")


def _is_noise(path: str) -> bool:
    """`/health`, `/health/ready` and `/metrics`; nothing else by prefix."""
    return path in _UNLOGGED or path.startswith("/health/")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response
