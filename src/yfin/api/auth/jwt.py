"""Minting and verifying access tokens.

Issuing a claim and *verifying* it are different things, and PyJWT will
happily do the first without the second: leave `audience=`/`issuer=` off
`decode` and those claims are decoration. That is how a staging key
shared with production -- a routine accident -- turns into staging tokens
that work in production. So the verification options here are explicit
and not optional.

The algorithm list is a fixed single entry for the same reason. When
RS256 arrives (the `kid` header exists so it can, without breaking issued
tokens), it must be a second branch keyed on `kid`, never a second entry
in this list: accepting HS256 and RS256 together is the classic
confusion attack, where the public key is replayed as an HMAC secret.

`kid` is attacker-controlled input. It only ever indexes a dict built in
this process -- never a file path, never a query.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

import jwt
from jwt import InvalidTokenError

from yfin.api.core.config import ApiSettings

ALGORITHM = "HS256"

#: Claims that must be present. A token missing any of them is rejected
#: before its values are looked at.
REQUIRED_CLAIMS = ("exp", "iat", "iss", "aud", "sub", "jti", "sid", "epc")

#: Clock skew tolerance. With a 15-minute lifetime, a few seconds of
#: drift between two hosts must not reject a freshly minted token.
LEEWAY_SECONDS = 30


class TokenInvalid(Exception):
    """Verification failed. The reason stays internal."""


@dataclass(frozen=True)
class TokenClaims:
    client_id: str
    scopes: tuple[str, ...]
    secret_id: int
    epoch: int
    jti: str


def _keys(settings: ApiSettings) -> dict[str, bytes]:
    """kid -> key. One entry today; the shape is what allows rotation."""
    return {settings.jwt_kid: settings.signing_key_bytes()}


def mint(
    settings: ApiSettings,
    *,
    client_id: str,
    scopes: tuple[str, ...],
    secret_id: int,
    epoch: int,
) -> tuple[str, int]:
    """Returns the encoded token and its lifetime in seconds.

    `sid` and `epc` are what make revocation possible without a database
    read: they name the secret the token was minted from and the
    authorisation generation it belongs to.
    """
    now = int(time.time())
    expires_in = settings.token_ttl_seconds
    payload = {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "sub": client_id,
        "iat": now,
        "exp": now + expires_in,
        # Audit only. There is no replay protection to be had in a
        # read-only API, and claiming otherwise would be worse than not
        # having it: it links a request back to the moment of issue.
        "jti": uuid.uuid4().hex,
        "scope": " ".join(scopes),
        "sid": secret_id,
        "epc": epoch,
    }
    token = jwt.encode(
        payload,
        settings.signing_key_bytes(),
        algorithm=ALGORITHM,
        headers={"kid": settings.jwt_kid},
    )
    return token, expires_in


def verify(settings: ApiSettings, token: str) -> TokenClaims:
    keys = _keys(settings)
    try:
        header = jwt.get_unverified_header(token)
    except InvalidTokenError as exc:
        raise TokenInvalid("unreadable header") from exc

    key = keys.get(str(header.get("kid", "")))
    if key is None:
        # Unknown kid: reject before any signature work, and never let the
        # value reach a filesystem or a query.
        raise TokenInvalid("unknown kid")

    try:
        payload = jwt.decode(
            token,
            key,
            algorithms=[ALGORITHM],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            leeway=LEEWAY_SECONDS,
            options={"require": list(REQUIRED_CLAIMS)},
        )
    except InvalidTokenError as exc:
        raise TokenInvalid("rejected") from exc

    scope_claim = payload.get("scope", "")
    if not isinstance(scope_claim, str):
        raise TokenInvalid("malformed scope")
    try:
        secret_id = int(payload["sid"])
        epoch = int(payload["epc"])
    except (TypeError, ValueError) as exc:
        raise TokenInvalid("malformed sid/epc") from exc

    return TokenClaims(
        client_id=str(payload["sub"]),
        scopes=tuple(s for s in scope_claim.split(" ") if s),
        secret_id=secret_id,
        epoch=epoch,
        jti=str(payload["jti"]),
    )
