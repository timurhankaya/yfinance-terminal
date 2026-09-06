"""The FastAPI application.

Assembly only: routers, middleware, error handlers. Every decision worth
arguing about lives in the module it belongs to.

Middleware order matters and is not arbitrary. Starlette runs them
outermost-first, so `RequestContextMiddleware` is added last in order to
run first: the request id it assigns has to exist before anything else
can log or fail with it.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from yfin.api.core.config import ApiSettings, get_api_settings
from yfin.api.core.errors import install_error_handlers
from yfin.api.core.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from yfin.api.routers import meta

TITLE = "yfin Data API"
VERSION = "1.0.0"
DESCRIPTION = (
    "Read-only access to the yfin market data warehouse. "
    "Authenticate with the OAuth2 client credentials flow."
)


def create_app(settings: ApiSettings | None = None) -> FastAPI:
    settings = settings or get_api_settings()

    app = FastAPI(
        title=TITLE,
        version=VERSION,
        description=DESCRIPTION,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )

    # Routers read settings from here, never from the module-level cache:
    # otherwise create_app(settings) would be silently ignored and every
    # app in a process would share one configuration.
    app.state.api_settings = settings

    install_error_handlers(app)

    origins = settings.cors_origin_list()
    if origins:
        # Off unless configured: a public read API is called server to
        # server by default, and a wildcard here would be a standing
        # invitation to burn someone else's quota from a browser.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET"],
            allow_headers=["Authorization", "If-None-Match"],
        )

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware, settings=settings)

    app.include_router(meta.router)
    return app


app = create_app()
