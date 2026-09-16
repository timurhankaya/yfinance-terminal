"""Authenticating a request and enforcing scope: a signature check plus one
Redis read (disabled client, moved epoch, revoked secret), no database.
Redis down fails open, logged at error; the window is bounded by the token
lifetime. Scopes are declared via `Security(...)` so the OpenAPI document
and the enforced check come from one declaration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request, Security
from fastapi.openapi.models import OAuthFlowClientCredentials, OAuthFlows
from fastapi.security import OAuth2, SecurityScopes

from yfin.api.auth.jwt import TokenClaims, TokenInvalid, verify
from yfin.api.core.config import ApiSettings
from yfin.api.core.errors import (
    TYPE_CLIENT_DISABLED,
    TYPE_INSUFFICIENT_SCOPE,
    TYPE_INVALID_TOKEN,
    TYPE_UNAUTHENTICATED,
    ApiProblem,
)
from yfin.api.models.clients import ApiScope
from yfin.api.ratelimit.connection import get_redis
from yfin.api.ratelimit.revocation import disabled_key, epoch_key, revoked_secret_key
from yfin.core.families import DataFamily, scope_for
from yfin.core.logging_setup import get_logger

log = get_logger(__name__)

REALM = "yfin-api"
TOKEN_URL = "/oauth/token"
BEARER_PREFIX = "bearer "

SCOPE_DESCRIPTIONS = {scope.value: f"Read {scope.name.lower()} data" for scope in ApiScope}

#: Declared so `/docs` renders a working Authorize button. auto_error is
#: off because the 401 body and its WWW-Authenticate header are built
#: here, not by the default handler.
oauth2_scheme = OAuth2(
    flows=OAuthFlows(
        clientCredentials=OAuthFlowClientCredentials(
            tokenUrl=TOKEN_URL, scopes=SCOPE_DESCRIPTIONS
        )
    ),
    scheme_name="clientCredentials",
    description=(
        "OAuth2 client credentials. POST your client id and secret as HTTP "
        "Basic to `/oauth/token` and send the returned token as "
        "`Authorization: Bearer <token>`. The token url is relative to this "
        "document's own URL, so a saved copy of the file has no base to "
        "resolve it against -- use the `servers` entry in that case."
    ),
    auto_error=False,
)


#: The web terminal's principal. Not a row in `api_clients`, never
#: metered (see `ratelimit/dependencies.meter`), and it holds every read
#: scope: the single operator behind the UI owns the data.
UI_CLIENT_ID = "ui"
UI_PAGE_CAP = 1000
UI_SCOPES = frozenset(scope_for(f) for f in DataFamily)


@dataclass(frozen=True)
class Principal:
    """The authenticated caller."""

    client_id: str
    scopes: frozenset[str]
    jti: str


#: Sentinel epoch no token can satisfy, used to express "this specific
#: credential was revoked" through the same comparison as a stale epoch.
_FORCE_STALE = 1 << 62


def _unauthenticated() -> ApiProblem:
    """No credentials at all. No `error` parameter: RFC 6750 §3 reserves
    those for a request that actually presented something."""
    return ApiProblem(
        401,
        TYPE_UNAUTHENTICATED,
        "Authentication required",
        headers={"WWW-Authenticate": f'Bearer realm="{REALM}"'},
    )


def _invalid_token(problem_type: str = TYPE_INVALID_TOKEN) -> ApiProblem:
    """Told apart from `insufficient_scope` on purpose: a client that
    cannot tell "expired, refresh" from "not allowed, refreshing will not
    help" ends up in a refresh loop against the most expensive endpoint
    in the API."""
    return ApiProblem(
        401,
        problem_type,
        "The access token is not valid",
        headers={
            "WWW-Authenticate": (
                f'Bearer realm="{REALM}", error="invalid_token", '
                'error_description="the token is expired or has been revoked"'
            )
        },
    )


def insufficient_scope(scope: str) -> ApiProblem:
    """Public: the generic dataset route raises the same 403, and a second
    copy of the body and its challenge header is how the two drift."""
    return ApiProblem(
        403,
        TYPE_INSUFFICIENT_SCOPE,
        "The token does not carry the required scope",
        detail=f"required scope: {scope}",
        headers={"WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{scope}"'},
    )


def _revocation_state(settings: ApiSettings, claims: TokenClaims) -> tuple[bool, int | None]:
    """(disabled, published_epoch). Fails open, loudly."""
    try:
        client = get_redis(settings)
        disabled, epoch, revoked = client.mget(
            disabled_key(claims.client_id),
            epoch_key(claims.client_id),
            revoked_secret_key(claims.secret_id),
        )
    except Exception as exc:  # noqa: BLE001 - see the module docstring
        log.error("revocation_check_failed_open", error=str(exc))
        return False, None

    if revoked is not None:
        # Revoking a credential kills the tokens already minted from it,
        # not just future authentications.
        return False, _FORCE_STALE
    return disabled is not None, int(epoch) if epoch is not None else None


def current_principal(
    request: Request,
    security_scopes: SecurityScopes,
    header: Annotated[str | None, Depends(oauth2_scheme)],
) -> Principal:
    settings: ApiSettings = request.app.state.api_settings

    if not header:
        # A Bearer token is the only credential `/v1` knows. The terminal
        # does not reach here at all: its mount overrides this dependency
        # with a fixed public principal (`yfin.ui.public.ui_principal`).
        raise _unauthenticated()
    if not header.lower().startswith(BEARER_PREFIX):
        raise _invalid_token()

    try:
        claims = verify(settings, header[len(BEARER_PREFIX) :].strip())
    except TokenInvalid as exc:
        raise _invalid_token() from exc

    disabled, published_epoch = _revocation_state(settings, claims)
    if disabled:
        raise _invalid_token(TYPE_CLIENT_DISABLED)
    if published_epoch is not None and claims.epoch < published_epoch:
        raise _invalid_token()

    held = frozenset(claims.scopes)
    # Scope before existence, always: the reverse order lets a caller
    # enumerate what exists by reading 404 against 403.
    for required in security_scopes.scopes:
        if required not in held:
            raise insufficient_scope(required)

    request.state.client_id = claims.client_id
    request.state.jti = claims.jti
    return Principal(client_id=claims.client_id, scopes=held, jti=claims.jti)


#: For endpoints that need a caller but no particular scope.
Authenticated = Annotated[Principal, Security(current_principal)]
