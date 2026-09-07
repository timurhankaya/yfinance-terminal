"""The terminal's own data mount: the `/v1` routers, served again under
`/ui/api/v1` without the OAuth2 gate.

Why a second mount rather than opening `/v1` itself: `/v1` is the
metered, contracted API that paying clients call with a Bearer token, and
its `openapi.json` is locked in CI. The browser page needs the same
reads without a credential, so it gets the same routers on a sub-app
whose only differences are the principal (a fixed `ui` identity that the
metering layer already skips) and a per-IP brake. The public contract,
its quotas and its document do not change.

The terminal is public: every request on this mount runs as that one
identity, with nothing to present and nothing to check.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response

from yfin.api.auth.dependencies import UI_CLIENT_ID, UI_SCOPES, Principal, current_principal
from yfin.api.core.config import ApiSettings
from yfin.api.core.errors import (
    TYPE_RATE_LIMIT,
    install_error_handlers,
    problem_response,
)
from yfin.api.core.middleware import SettingsMiddleware, resolve_client_ip, trusted_networks
from yfin.api.ratelimit.fixed_window import FixedWindow
from yfin.api.routers.v1 import datasets, market

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
    """Everyone is the `ui` principal. Nothing is presented and nothing is
    checked: this mount exists to serve the browser page the same reads
    `/v1` serves, off the metered path."""
    request.state.client_id = UI_CLIENT_ID
    request.state.jti = PUBLIC_PRINCIPAL.jti
    return PUBLIC_PRINCIPAL


#: Everything the browser page calls, the mirror and the UI-only routes
#: alike. Installed on the outer app, because the UI-only routes
#: (`/ui/api/symbols/{s}/news`, later `/ticks` and `/gaps`) are registered
#: outside the mount and a brake inside it would never see them: an
#: unmetered, unbraked database query a page away.
BRAKE_PREFIX = "/ui/api"


class RequestBrake(SettingsMiddleware):
    """Per-IP fixed window over every request under BRAKE_PREFIX; anything
    else passes untouched (`/v1` has its own limiter).

    It resolves the address itself rather than reading
    `request.state.client_ip`. `add_middleware` prepends, so this one --
    added last, from `install` -- runs OUTSIDE `RequestContextMiddleware`
    and that attribute does not exist yet. Reading it would have every
    request in the world share the "unknown" bucket, which is a brake
    that is either off or shut, never per address.
    """

    def __init__(self, app: Callable[..., object], settings: ApiSettings) -> None:
        super().__init__(app, settings)
        self._limit = settings.ui_requests_per_minute
        self._nets = trusted_networks(settings)
        self.window = FixedWindow()

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not request.url.path.startswith(BRAKE_PREFIX):
            return await call_next(request)
        client_ip = resolve_client_ip(request, self._nets)
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
    api.dependency_overrides[current_principal] = ui_principal
    api.include_router(market.router)
    api.include_router(datasets.router)
    return api
