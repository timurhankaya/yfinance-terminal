"""The terminal's own data mount: the `/v1` routers, served again under
`/ui/api/v1` without the OAuth2 gate.

Why a second mount rather than opening `/v1` itself: `/v1` is the
metered, contracted API that paying clients call with a Bearer token, and
its `openapi.json` is locked in CI. The browser page needs the same
reads without a credential, so it gets the same routers on a sub-app
whose only differences are the principal (a fixed `ui` identity that the
metering layer already skips) and a per-IP brake. The public contract,
its quotas and its document do not change.

In password mode the mount stays, but the principal comes from the
session cookie; in public mode it is granted to everyone.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from yfin.api.auth.dependencies import UI_CLIENT_ID, UI_SCOPES, Principal, current_principal
from yfin.api.core.config import ApiSettings
from yfin.api.core.errors import (
    TYPE_INVALID_TOKEN,
    TYPE_RATE_LIMIT,
    TYPE_UNAUTHENTICATED,
    ApiProblem,
    install_error_handlers,
    problem_response,
)
from yfin.api.ratelimit.fixed_window import FixedWindow
from yfin.api.routers.v1 import datasets, market
from yfin.ui import session
from yfin.ui.session import COOKIE_NAME, SessionInvalid

#: The routers keep their own `/v1` prefix, so mounting at /ui/api puts
#: them at /ui/api/v1/... -- the same paths as the public API, one level
#: down. The sub-app is also the 404 for any unknown /ui/api path: it is
#: mounted after the terminal's own routes, and anything the routers do
#: not know becomes a problem document here rather than index.html.
MOUNT_PATH = "/ui/api"

#: The identity every public request runs as. `meter` skips metering for
#: this client id and caps page size at UI_PAGE_CAP.
PUBLIC_PRINCIPAL = Principal(client_id=UI_CLIENT_ID, scopes=UI_SCOPES, jti="public")


def ui_principal(request: Request) -> Principal:
    """Public mode: everyone is the `ui` principal. Password mode: the
    session cookie must be present and valid; a Bearer token is not
    accepted here because this mount exists precisely to bypass metering."""
    settings: ApiSettings = request.app.state.api_settings
    if settings.ui_public:
        request.state.client_id = UI_CLIENT_ID
        request.state.jti = PUBLIC_PRINCIPAL.jti
        return PUBLIC_PRINCIPAL
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        raise ApiProblem(401, TYPE_UNAUTHENTICATED, "Login required")
    try:
        claims = session.verify(settings, raw)
    except SessionInvalid as exc:
        raise ApiProblem(401, TYPE_INVALID_TOKEN, "The session is not valid") from exc
    request.state.client_id = UI_CLIENT_ID
    request.state.jti = claims.jti
    return Principal(client_id=UI_CLIENT_ID, scopes=UI_SCOPES, jti=claims.jti)


class RequestBrake(BaseHTTPMiddleware):
    """Per-IP fixed window over everything on this mount."""

    def __init__(self, app: Callable[..., object], settings: ApiSettings) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._limit = settings.ui_requests_per_minute
        self.window = FixedWindow()

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        client_ip = getattr(request.state, "client_ip", "unknown")
        if not self.window.allow(client_ip, self._limit):
            return problem_response(
                request,
                429,
                TYPE_RATE_LIMIT,
                "Too many requests from this address",
                detail=f"at most {self._limit} terminal requests per minute",
                headers={"Retry-After": "60"},
            )
        return await call_next(request)


def build_data_api(settings: ApiSettings) -> FastAPI:
    """The sub-application mounted at MOUNT_PATH. No OpenAPI document of
    its own: the contract is `/v1`'s, and this is the same surface."""
    api = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    api.state.api_settings = settings
    install_error_handlers(api)
    api.add_middleware(RequestBrake, settings=settings)
    api.dependency_overrides[current_principal] = ui_principal
    api.include_router(market.router)
    api.include_router(datasets.router)
    return api
