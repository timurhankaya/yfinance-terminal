"""Server-rendered admin forms over the `settings` table, proxy pool and screens.
Writes go through the same validated paths as `yfin config` / `yfin proxy`.
Off unless `YFAPI_ADMIN_PASSWORD` is set; HTTP Basic sends the secret with
every request, so serve it behind TLS."""

from __future__ import annotations

from fastapi import FastAPI

from yfin.api.core.config import ApiSettings
from yfin.core.logging_setup import get_logger

log = get_logger(__name__)

MOUNT_PATH = "/admin"


def install(app: FastAPI, settings: ApiSettings) -> None:
    """Registers the routes. The caller checks `settings.admin_password`;
    an empty secret means no admin page at all, not an open one."""
    from yfin.admin import router

    assert settings.admin_password, "install() with no admin secret"
    app.include_router(router.router)
    log.info("admin_enabled", path=MOUNT_PATH)
