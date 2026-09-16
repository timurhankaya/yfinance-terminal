"""The web terminal: a browser UI served by the API process under /ui.
Imported only when `YFAPI_UI_ENABLED` is on; `create_app` guards it."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from yfin.api.core.config import ApiSettings
from yfin.core.logging_setup import get_logger

log = get_logger(__name__)


def install(app: FastAPI, settings: ApiSettings, dist_dir: Path | None = None) -> None:
    """Mounts the UI. Registration order matters: the terminal's own /ui/api
    routes, then the `/v1` mirror mount (which would swallow them), then
    the pages, so /ui/api/* never falls through to index.html. `/ui/ws` is
    a WebSocket scope: `RequestBrake` never sees it, so `live.py` carries
    its own Origin check and connection limits."""
    from yfin.core.config import get_settings
    from yfin.ui import data, live, pages, public

    # Warmed at startup: `get_settings()` lazily does a blocking database
    # read, and `/ui/ws` calls it from an `async` handler, so the first
    # handshake would otherwise stall the event loop.
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
    pages.install_pages(app, dist, dockview_enabled=settings.dockview_enabled)
