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
from yfin.api.core.docs import TAGS, description
from yfin.api.core.errors import install_error_handlers
from yfin.api.core.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from yfin.api.ratelimit.dependencies import UsageMiddleware
from yfin.api.routers import meta, oauth
from yfin.api.routers.v1 import datasets, market

TITLE = "yfin Data API"
VERSION = "1.0.0"


def create_app(settings: ApiSettings | None = None) -> FastAPI:
    settings = settings or get_api_settings()

    app = FastAPI(
        title=TITLE,
        version=VERSION,
        # Generated from the catalogue, so the document cannot list a
        # resource the API does not serve or miss one it does.
        description=description(),
        openapi_tags=TAGS,
        # Two views of the same document, because they answer different
        # questions. Swagger UI is where a developer pastes a client id
        # and calls an endpoint; ReDoc is where they read the contract
        # end to end. Both render from /openapi.json, so neither can
        # drift from what the API actually serves.
        #
        # Both pull their assets from a CDN. On a host without outbound
        # internet the pages load but stay blank -- the document itself
        # is always available at /openapi.json, which is what tooling
        # consumes anyway.
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
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

    # Inside the security headers, outside the routes: it has to run
    # after the handler so it can see the final status code, and it
    # must run even when the handler raised, to release the slot.
    app.add_middleware(UsageMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware, settings=settings)

    app.include_router(meta.router)
    app.include_router(oauth.router)
    app.include_router(market.router)
    app.include_router(datasets.router)
    return app


app = create_app()
