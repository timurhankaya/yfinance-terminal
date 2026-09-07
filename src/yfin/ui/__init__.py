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
    """Mounts the UI. Order matters: three layers, in this order: the
    data routes, then the API router (which ends with a catch-all 404 for
    /ui/api/*), then the pages. Starlette matches in registration order,
    so a data route must be registered before the catch-all would swallow
    it, and the catch-all before the pages so /ui/api/* can never fall
    through to index.html."""
    from yfin.ui import data, pages, router

    # Three layers, in this order: the data routes, then the API router
    # (which ends with a catch-all 404 for /ui/api/*), then the pages.
    # Starlette matches in registration order.
    app.include_router(data.router)
    app.include_router(router.router)

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
