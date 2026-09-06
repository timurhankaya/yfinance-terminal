"""`POST /oauth/token` -- the OAuth2 client credentials grant.

This endpoint is the single exception to the API's error format, and the
exception is not cosmetic. RFC 6749 §5.2 defines its own error body, and
that is what every OAuth2 client library parses: authlib,
requests-oauthlib, Go's clientcredentials, the Authorize button in
`/docs`. Hand them an RFC 9457 problem document and they find no `error`
field, cannot tell `invalid_client` from `invalid_scope`, and typically
raise "unknown error" or retry forever. Worse, a contract test would not
catch it, because the document would match the schema we published.

Everything else here follows from one fact: at the moment this code runs,
`client_id` is unverified text supplied by whoever is calling. So the
rate limit is keyed on the address (`ratelimit/token_endpoint.py`), the
work done is the same whether the client exists or not, and every failure
mode -- unknown client, wrong credential, revoked credential, disabled
client -- produces the identical status, body and headers.
"""

from __future__ import annotations

import base64
import binascii
from typing import Annotated
from urllib.parse import unquote_plus

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from yfin.api.auth.hashing import verify_against
from yfin.api.auth.jwt import mint
from yfin.api.core.config import ApiSettings
from yfin.api.ratelimit import token_endpoint as limiter
from yfin.api.storage import clients as repo
from yfin.api.storage.session import session_scope
from yfin.core.logging_setup import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["oauth"])

REALM = "yfin-api"
GRANT_TYPE = "client_credentials"

#: One message for every authentication failure. Different wording per
#: cause would answer, in text, the questions the constant-time
#: verification path exists to keep unanswered.
INVALID_CLIENT_MESSAGE = "client authentication failed"


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    scope: str


def _oauth_error(
    error: str,
    description: str,
    *,
    status: int,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """RFC 6749 §5.2 error body -- not problem+json."""
    all_headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    all_headers.update(headers or {})
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status,
        headers=all_headers,
    )


def _invalid_client() -> JSONResponse:
    return _oauth_error(
        "invalid_client",
        INVALID_CLIENT_MESSAGE,
        status=401,
        headers={"WWW-Authenticate": f'Basic realm="{REALM}", charset="UTF-8"'},
    )


def _parse_basic(header: str) -> tuple[str, str] | None:
    """Decodes a Basic header per RFC 6749 §2.3.1.

    The percent-decoding step is easy to skip and would appear to work,
    because our own credentials are URL-safe base64 and survive it
    unchanged. It is here for the client that does follow the spec:
    without it, a credential containing an encoded character would fail
    authentication for no visible reason.
    """
    if not header.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    if ":" not in raw:
        return None
    identifier, credential = raw.split(":", 1)
    return unquote_plus(identifier), unquote_plus(credential)


@router.post(
    "/oauth/token",
    summary="Issue an access token",
    response_model=TokenResponse,
    responses={
        400: {"description": "invalid_request | unsupported_grant_type | invalid_scope"},
        401: {"description": "invalid_client"},
        429: {"description": "too many attempts"},
        503: {"description": "authentication temporarily unavailable"},
    },
)
def issue_token(
    request: Request,
    session: Annotated[Session, Depends(session_scope)],
    grant_type: Annotated[str, Form()],
    scope: Annotated[str | None, Form()] = None,
    posted_credential: Annotated[str | None, Form(alias="client_secret")] = None,
) -> JSONResponse:
    settings: ApiSettings = request.app.state.api_settings
    client_ip = getattr(request.state, "client_ip", "unknown")

    if posted_credential is not None:
        # client_secret_post is not supported. Saying so plainly beats a
        # generic failure the caller would read as "wrong credential" and
        # then chase in the wrong place.
        return _oauth_error(
            "invalid_request",
            "client credentials must be sent in the Authorization header (Basic)",
            status=400,
        )

    credentials = _parse_basic(request.headers.get("authorization", ""))
    if credentials is None:
        return _oauth_error(
            "invalid_request",
            "a Basic Authorization header is required",
            status=400,
            headers={"WWW-Authenticate": f'Basic realm="{REALM}", charset="UTF-8"'},
        )
    client_id, presented = credentials

    decision = limiter.check(settings, client_ip=client_ip, client_id=client_id)
    if not decision.allowed:
        if decision.degraded:
            return _oauth_error(
                "temporarily_unavailable",
                "authentication is rate limited while a dependency is unavailable",
                status=503,
                headers={"Retry-After": str(decision.retry_after)},
            )
        return _oauth_error(
            "slow_down",
            "too many token requests",
            status=429,
            headers={"Retry-After": str(decision.retry_after)},
        )

    if grant_type != GRANT_TYPE:
        return _oauth_error(
            "unsupported_grant_type",
            f"only {GRANT_TYPE} is supported",
            status=400,
        )

    record = repo.load_for_auth(session, client_id)
    # An unknown client still pays for two verifications; the empty
    # candidate list is padded with decoys inside verify_against.
    credential_id = verify_against(presented, record.candidates if record else [])

    if record is None or credential_id is None or not record.is_active:
        limiter.record_failure(settings, client_id)
        log.info("token_denied", client_ip=client_ip)
        return _invalid_client()

    granted = record.scopes
    if scope is not None:
        requested = tuple(s for s in scope.split(" ") if s)
        if not set(requested) <= set(record.scopes):
            # Named separately from invalid_client on purpose: the caller
            # authenticated, so this is a fixable request, not a rejected
            # identity.
            return _oauth_error(
                "invalid_scope",
                "the requested scope exceeds what this client is granted",
                status=400,
            )
        granted = requested

    token, expires_in = mint(
        settings,
        client_id=record.client_id,
        scopes=granted,
        secret_id=credential_id,
        epoch=record.auth_epoch,
    )
    limiter.record_success(settings, client_id)

    body = TokenResponse(access_token=token, expires_in=expires_in, scope=" ".join(granted))
    return JSONResponse(
        body.model_dump(),
        # RFC 6749 §5.1: a token response must never be cached.
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )
