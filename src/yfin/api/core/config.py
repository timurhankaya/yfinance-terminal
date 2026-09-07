"""API settings.

Deliberately a separate BaseSettings from `yfin.core.config.Settings`,
for two reasons. These are not pipeline settings managed by the
`settings` table -- an operator changing `yf_max_shards` from the admin
panel has no business changing the JWT audience. And the signing key is a
secret: like `yf_proxy_secret_key` it stays in the environment and never
becomes an editable row.

The `YFAPI_` prefix is here to separate the two namespaces. Note that the
pipeline's `Settings` uses no prefix at all (its fields are already
`yf_*`/`db_*`), so this is not a repeat of an existing pattern.
"""

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
    # Off by default: a deployment that has not opted in serves nothing
    # under /ui and never imports yfin.ui. The password is a plain string
    # in the environment on purpose -- one operator, one secret, nothing
    # a hash would protect (see the web terminal design, "Kimlik
    # doğrulama").
    ui_enabled: bool = False
    ui_password: str = ""

    # --- health -----------------------------------------------------------
    health_cache_seconds: int = Field(default=5, ge=0)
    health_rate_limit_per_minute: int = Field(default=60, ge=1)

    def signing_key_bytes(self) -> bytes:
        """The signing key, refusing anything too short.

        Checked where the key is used rather than at field level so that
        commands and health checks that never sign a token still run
        without one configured.
        """
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

    def validate_ui(self) -> None:
        """Refuses a UI that is switched on with nothing guarding it.

        Called from `create_app`, not at field level, for the same reason
        as `signing_key_bytes`: a CLI command that never serves the UI
        must not fail because the UI is misconfigured.
        """
        if self.ui_enabled and not self.ui_password:
            raise ValueError("YFAPI_UI_ENABLED is on but YFAPI_UI_PASSWORD is empty")

    def ui_cookie_secure(self) -> bool:
        """`Secure` only when the deployment says it is behind TLS. An empty
        base URL means plain HTTP on localhost, where a Secure cookie is
        silently dropped by the browser and login appears to do nothing."""
        return self.public_base_url.lower().startswith("https://")


@functools.lru_cache(maxsize=1)
def get_api_settings() -> ApiSettings:
    return ApiSettings()
