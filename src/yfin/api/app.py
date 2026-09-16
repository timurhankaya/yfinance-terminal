"""The FastAPI application: assembly of routers, middleware and error handlers.
Starlette runs middleware outermost-first, so `RequestContextMiddleware` is
added last to run first: its request id must exist before anything else
logs or fails with it."""

from __future__ import annotations

from importlib.metadata import version
from typing import Any

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
from yfin.core.config import bootstrap_settings
from yfin.core.logging_setup import configure_logging, get_logger
from yfin.core.metrics import set_build_info
from yfin.core.tracing import configure_tracing, instrument_fastapi

log = get_logger(__name__)

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
    """Swagger UI and ReDoc with our icon. Excluded from the OpenAPI document,
    which also keeps them out of `OPERATION_IDS` and the response tables."""

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


def build_instrumentator(registry: object | None = None) -> Any:
    """The instrumentator, configured; None without the package. `registry` is
    for tests: the default one is process-wide and a duplicate registration
    silently attaches no instrumentation, so a second app in one process
    records nothing. In-progress gauges are meaningless under
    `PROMETHEUS_MULTIPROC_DIR`, hence off."""
    try:
        from prometheus_fastapi_instrumentator import Instrumentator
    except ImportError as exc:  # pragma: no cover - the package is in [api]
        log.warning("http metrics not installed", error=str(exc))
        return None

    kwargs: dict[str, Any] = {} if registry is None else {"registry": registry}
    return Instrumentator(
        should_group_status_codes=False,
        should_instrument_requests_inprogress=False,
        excluded_handlers=["/metrics", "/health"],
        **kwargs,
    )


def _install_metrics(app: FastAPI) -> None:
    """HTTP metrics and the `/metrics` endpoint. Must run after the routers:
    the `handler` label comes from the route template. Unauthenticated and
    renders every series, so it carries the same per-IP cap as
    `/health/ready`."""
    from fastapi import Depends

    instrumentator = build_instrumentator()
    if instrumentator is None:
        return

    from yfin.api.core.window import metrics_window

    instrumentator.instrument(app, metric_namespace="yfin")
    instrumentator.expose(
        app,
        include_in_schema=False,
        dependencies=[Depends(metrics_window)],
    )


def create_app(settings: ApiSettings | None = None) -> FastAPI:
    settings = settings or get_api_settings()

    # Before uvicorn installs its own handlers, or the access log would
    # render through a chain that does not redact. `bootstrap_settings`
    # because `create_app` also runs in `dump_openapi.py` with no database.
    configure_logging(
        bootstrap_settings().log_level, bootstrap_settings().log_format, "api"
    )

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
        # Swagger UI and ReDoc are served by hand below so they can carry
        # our own favicon; the OpenAPI route stays FastAPI's.
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

    _install_metrics(app)
    # After the routers, like the instrumentator: the span name is the
    # route template, so the routes have to exist first.
    configure_tracing("api")
    instrument_fastapi(app)
    set_build_info(version("yfin"))

    if settings.ui_enabled:
        # Imported here, not at module level, so a deployment with the UI
        # off never loads it.
        from yfin.ui import install as install_ui

        install_ui(app, settings)

    if settings.admin_password:
        # Same rule as the terminal: imported only when switched on.
        from yfin.admin import install as install_admin

        install_admin(app, settings)

    # After the routers, because it names every route and builds the
    # document from them. Installed even when the docs are withheld: a
    # deployment that does not publish the contract must still BE the
    # application the committed contract describes.
    openapi_document.install(app)
    return app


app = create_app()
