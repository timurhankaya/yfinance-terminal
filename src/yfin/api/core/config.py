"""API settings, separate from `yfin.core.config.Settings`: these are not
pipeline settings managed by the `settings` table, and the signing key is a
secret that stays in the environment. The `YFAPI_` prefix separates the
two namespaces."""

from __future__ import annotations

import functools

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from yfin.core.text import comma_list

# Below this, an HS256 key is weaker than the digest it feeds.
MIN_SIGNING_KEY_BYTES = 32


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="YFAPI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- token ------------------------------------------------------------
    # No default: an empty key must fail loudly at startup, not sign
    # tokens that anyone can forge.
    jwt_signing_key: str = ""
    jwt_kid: str = "k1"
    jwt_issuer: str = "yfin-api"
    jwt_audience: str = "yfin-api"
    token_ttl_seconds: int = Field(default=900, ge=60, le=3600)

    # --- infrastructure ---------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"

    # --- network ----------------------------------------------------------
    # CIDR list. Empty means "no reverse proxy": X-Forwarded-For is then
    # ignored entirely rather than trusted blindly.
    trusted_proxies: str = ""
    cors_origins: str = ""
    docs_enabled: bool = True
    #: The base URL this deployment answers on, published as the first
    #: `servers` entry. Empty means the constant in `core/openapi.py`,
    #: which is what the committed openapi.json carries -- the document
    #: must not differ by whose machine generated it.
    public_base_url: str = ""

    # --- web terminal -----------------------------------------------------
    # Switching this on publishes the terminal to anyone who can reach the
    # port: no login, an unmetered mount (`/ui/api/v1`), and only the
    # per-address brake below in front of it.
    ui_enabled: bool = False
    #: Split-panel workspaces. Read into the HTML shell at API startup so
    #: changing this value needs no Vite rebuild.
    dockview_enabled: bool = True
    #: Per client IP, per process, over everything under /ui/api. A crude
    #: brake on one browser's worth of traffic, not the API's limiter.
    ui_requests_per_minute: int = Field(default=600, ge=1)
    #: New `/ui/ws` sockets one address may open per minute, per process.
    #: A WebSocket never passes through `RequestBrake` (BaseHTTPMiddleware,
    #: different scope type), so the `/ui/api` brake does not cover it.
    ui_ws_connections_per_minute: int = Field(default=30, ge=1)
    #: Sockets this process serves at once, across all addresses. Each one
    #: holds a Redis pub/sub connection, and each `sub` frame takes a
    #: threadpool thread and a database session -- both of which are the
    #: PROCESS's, shared with `/v1`. The ceiling is what keeps the free
    #: terminal from spending the paid surface's resources.
    ui_ws_max_connections: int = Field(default=200, ge=1)

    # --- admin page -------------------------------------------------------
    #: Switches the admin page (/admin) on: settings table, proxy pool,
    #: screens, API clients. Empty (the default) means no such routes at
    #: all. One operator, one secret, HTTP Basic; serve it behind TLS.
    admin_password: str = ""

    # --- health -----------------------------------------------------------
    health_cache_seconds: int = Field(default=5, ge=0)
    health_rate_limit_per_minute: int = Field(default=60, ge=1)

    def signing_key_bytes(self) -> bytes:
        """The signing key, refusing anything too short. Checked at use rather
        than at field level so commands that never sign a token run without
        one configured."""
        raw = self.jwt_signing_key.encode("utf-8")
        if len(raw) < MIN_SIGNING_KEY_BYTES:
            raise ValueError(
                f"YFAPI_JWT_SIGNING_KEY must be at least {MIN_SIGNING_KEY_BYTES} bytes"
            )
        return raw

    def cors_origin_list(self) -> list[str]:
        return comma_list(self.cors_origins)

    def trusted_proxy_list(self) -> list[str]:
        return comma_list(self.trusted_proxies)


@functools.lru_cache(maxsize=1)
def get_api_settings() -> ApiSettings:
    return ApiSettings()
