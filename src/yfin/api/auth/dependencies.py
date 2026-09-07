"""Authenticating a request and enforcing scope.

Two things happen per request and both are cheap by design: a signature
check, and one Redis read that the rate limiter needs anyway. No database
query is on this path.

That single Redis read is what closes the gap left by not querying the
database. It answers three questions at once -- is the client disabled,
has its authorisation generation moved on, was this specific credential
revoked -- so a leaked credential or a narrowed scope stops working in
seconds rather than at the end of the token's life.

If Redis is unreachable the check fails open, deliberately: refusing all
traffic because a counter store is down would be a far larger outage than
the window it protects, and that window is bounded by the token lifetime
anyway. Every occurrence is logged at error level.

Scopes are declared through FastAPI's `Security(...)`, which is what puts
them in the OpenAPI document. The same declaration is what this module
enforces, so the published contract and the running check cannot drift
apart -- an endpoint cannot advertise one scope and require another.
"""

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


def _insufficient_scope(scope: str) -> ApiProblem:
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
            raise _insufficient_scope(required)

    request.state.client_id = claims.client_id
    request.state.jti = claims.jti
    return Principal(client_id=claims.client_id, scopes=held, jti=claims.jti)


def scoped(*scopes: str) -> Principal:
    """Declares the scopes an endpoint needs, for OpenAPI and for the check."""
    return Security(current_principal, scopes=list(scopes))  # type: ignore[no-any-return]


#: For endpoints that need a caller but no particular scope.
Authenticated = Annotated[Principal, Security(current_principal)]
