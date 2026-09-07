"""The web terminal: a browser UI served by the API process under /ui.

Nothing here is imported unless `YFAPI_UI_ENABLED` is on -- `create_app`
guards the import -- so a deployment that has not opted in carries no
UI code path at all.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from yfin.api.core.config import ApiSettings
from yfin.core.logging_setup import get_logger

log = get_logger(__name__)


def install(app: FastAPI, settings: ApiSettings, dist_dir: Path | None = None) -> None:
    """Mounts the UI. Order matters: the terminal's own /ui/api routes
    first, then the `/v1` mirror mounted at /ui/api (it answers
    /ui/api/v1/* and is the 404 for every other /ui/api path), then the
    pages. Starlette matches in registration order, so the mount must
    come after the routes it would otherwise swallow, and before the pages
    so /ui/api/* can never fall through to index.html.

    `/ui/ws` sits outside all of that: it is a different scope type, so
    no HTTP route can shadow it and the `RequestBrake` below never sees
    it (a WebSocket does not pass through BaseHTTPMiddleware). Its guards
    are its own: the Origin check in `live.py`, plus the per-address and
    per-process connection limits there, which are what the brake would
    otherwise have provided."""
    from yfin.core.config import get_settings
    from yfin.ui import data, live, pages, public

    # Warmed here, at startup, and not for the value. `get_settings()`
    # lazily opens a database connection and reads the `settings` table
    # under a lock; `/ui/ws` calls it from an `async` handler, so the
    # FIRST handshake after startup would otherwise run that blocking
    # read on the event loop and stall every other request in the worker.
    get_settings()

    app.include_router(data.router)
    app.include_router(live.router)
    app.mount(public.MOUNT_PATH, public.build_data_api(settings), name="ui-data")
    # On the outer app so it covers the UI-only routes above as well as
    # the mount. `add_middleware` prepends, so added last it ends up
    # outermost -- ahead of RequestContextMiddleware, which is why it
    # resolves the client address itself (see RequestBrake).
    app.add_middleware(public.RequestBrake, settings=settings)

    dist = dist_dir if dist_dir is not None else pages.default_dist_dir()
    # Both halves, because StaticFiles raises in its constructor when the
    # directory is absent: a build with index.html but no assets/ would
    # otherwise take the API down at startup.
    if not (dist / "index.html").is_file() or not (dist / "assets").is_dir():
        # A checkout without `npm run build`: the API and its /ui/api
        # routes work, the page does not, and the log says why.
        log.warning("ui_build_missing", dist=str(dist))
        return
    pages.install_pages(app, dist)
