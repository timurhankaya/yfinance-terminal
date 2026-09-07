"""Request context, client IP resolution and security headers.

The client IP question is the load-bearing one here. Get it wrong in one
direction -- no proxy configuration -- and every request in the world
shares the reverse proxy's single address, so an IP-keyed limit either
blocks everyone or nobody. Get it wrong in the other -- trusting
`X-Forwarded-For` blindly -- and an attacker writes the header themselves
to slip past the limit, or writes a victim's address to get them blocked
and to poison the logs.

So the header is honoured only when the connection itself comes from a
configured proxy network, and the address is taken by walking the chain
from the right, skipping as many hops as we trust.
"""

from __future__ import annotations

import ipaddress
import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
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


def _networks(settings: ApiSettings) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
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
    request: Request, nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network]
) -> str:
    """The address rate limits and logs are keyed on.

    With no trusted networks configured the forwarded header is ignored
    outright -- an unconfigured deployment must not be a bypass.
    """
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


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id, binds log context, times the request.

    The log line carries the route *template*, never the query string: a
    client that puts a secret in the query string would otherwise write
    it into our logs, where it long outlives the request.
    """

    def __init__(self, app: Callable[..., object], settings: ApiSettings) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._nets = _networks(settings)

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
        log.info(
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


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response
