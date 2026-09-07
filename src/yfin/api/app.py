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
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import HTMLResponse

from yfin.api.core import openapi as openapi_document
from yfin.api.core.config import ApiSettings, get_api_settings
from yfin.api.core.docs import TAGS, description
from yfin.api.core.errors import install_error_handlers
from yfin.api.core.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from yfin.api.ratelimit.dependencies import UsageMiddleware
from yfin.api.ratelimit.revocation import mark_api_redis
from yfin.api.routers import meta, oauth
from yfin.api.routers.v1 import datasets, market

TITLE = "yfin Data API"
SUMMARY = "Read-only access to the yfin market data warehouse."
#: The DOCUMENT's version, which moves with the contract. `/v1` is the
#: SURFACE's version and moves only when the surface breaks; the two are
#: not the same number and the introduction says so.
VERSION = "1.0.0"

CONTACT = {"name": "Timurhan Kaya", "url": openapi_document.PRODUCTION_URL}

#: `identifier` is OpenAPI 3.1 only and is mutually exclusive with `url`:
#: a licence link cannot be added here without removing the SPDX id.
LICENSE = {"name": "AGPL-3.0-or-later", "identifier": "AGPL-3.0-or-later"}


def _install_documentation_pages(app: FastAPI) -> None:
    """Swagger UI and ReDoc, with our icon instead of FastAPI's.

    Both are excluded from the OpenAPI document -- they are how the
    contract is read, not part of it -- which is also what keeps them out
    of `OPERATION_IDS` and the response tables.
    """

    @app.get("/docs", include_in_schema=False)
    def swagger_ui() -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url="/openapi.json",
            title=f"{TITLE} — reference",
            oauth2_redirect_url="/docs/oauth2-redirect",
            swagger_favicon_url=openapi_document.FAVICON,
        )

    @app.get("/docs/oauth2-redirect", include_in_schema=False)
    def swagger_ui_redirect() -> HTMLResponse:
        return get_swagger_ui_oauth2_redirect_html()

    @app.get("/redoc", include_in_schema=False)
    def redoc() -> HTMLResponse:
        return get_redoc_html(
            openapi_url="/openapi.json",
            title=f"{TITLE} — contract",
            redoc_favicon_url=openapi_document.FAVICON,
        )


def create_app(settings: ApiSettings | None = None) -> FastAPI:
    settings = settings or get_api_settings()

    app = FastAPI(
        title=TITLE,
        version=VERSION,
        # Generated from the catalogue, so the document cannot list a
        # resource the API does not serve or miss one it does.
        description=description(),
        summary=SUMMARY,
        contact=CONTACT,
        license_info=LICENSE,
        servers=openapi_document.servers_for(settings),
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
        # The two HTML pages are served by hand below, so they can carry
        # our own favicon; the OpenAPI route is FastAPI's.
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )

    # Routers read settings from here, never from the module-level cache:
    # otherwise create_app(settings) would be silently ignored and every
    # app in a process would share one configuration.
    app.state.api_settings = settings

    if settings.docs_enabled:
        _install_documentation_pages(app)

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

    # Claims this Redis as the API's, so `yfin api client disable`
    # can refuse to publish a revocation into an unrelated one.
    mark_api_redis(settings)

    app.include_router(meta.router)
    app.include_router(oauth.router)
    app.include_router(market.router)
    app.include_router(datasets.router)

    # After the routers, because it names every route and builds the
    # document from them. Installed even when the docs are withheld: a
    # deployment that does not publish the contract must still BE the
    # application the committed contract describes.
    openapi_document.install(app)
    return app


app = create_app()
