"""The admin page: a few server-rendered forms over what an operator
changes at runtime -- the `settings` table, the proxy pool, which screens
run, and a read-only view of API clients.

Deliberately not a framework. `settings_store` was written so that "the
future admin panel will call the same functions" as `yfin config`; the
proxy operations mirror `yfin proxy` line for line. A generic CRUD admin
would have bypassed that validation and written raw rows.

Off unless `YFAPI_ADMIN_PASSWORD` is set. Guarded by HTTP Basic auth
(the browser's own prompt: no cookie, no session, no form to get
wrong), a per-address brake on failed attempts, and the same security
headers as the rest of the API. Serve it behind TLS: Basic sends the
secret with every request.
"""

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
