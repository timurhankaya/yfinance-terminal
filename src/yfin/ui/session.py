"""The UI session cookie.

It is a JWT signed with the API's key, and that is safe only because the
audience differs: `verify` in `api/auth/jwt.py` demands `aud=jwt_audience`
and `sid`/`epc`, so a session cookie presented as a Bearer token is
rejected, and the decode here demands `aud=yfin-ui`, so an access token
presented as a cookie is rejected too. Neither side can be replayed as
the other.

Stateless on purpose: `uvicorn --workers 4` gives four processes and no
shared memory, and a session table for one operator is a table nobody
would ever read.

The session is also bound to the password by a `pwf` claim (a truncated
SHA-256 fingerprint of `settings.ui_password`, never the password
itself). This is deliberate: for a single operator, rotating
`YFAPI_UI_PASSWORD` is the only incident response available (there is no
per-session revocation list), so it must invalidate every outstanding
session immediately, not just block new logins. Rotating
`YFAPI_JWT_SIGNING_KEY` also invalidates every session -- and, because
that key is shared with `api/auth/jwt.py`, every `/v1` access token too.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

import jwt
from jwt import InvalidTokenError

from yfin.api.auth.jwt import ALGORITHM, LEEWAY_SECONDS
from yfin.api.core.config import ApiSettings

COOKIE_NAME = "yfin_ui"
UI_AUDIENCE = "yfin-ui"
#: 24 hours, no sliding renewal: a fixed horizon is easier to reason
#: about than "as long as you keep using it", and one login a day is not
#: a burden on a single operator.
SESSION_TTL_SECONDS = 86_400

REQUIRED_CLAIMS = ("exp", "iat", "iss", "aud", "jti", "pwf")


def _password_fingerprint(password: str) -> str:
    """First 16 hex characters of SHA-256(password): enough to detect a
    rotation, not enough to be useful for anything else."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()[:16]


class SessionInvalid(Exception):
    """Verification failed. The reason stays internal."""


@dataclass(frozen=True)
class UiClaims:
    jti: str
    expires_at: int


def issue(settings: ApiSettings) -> tuple[str, int]:
    """The encoded cookie value and its expiry as a UNIX timestamp."""
    now = int(time.time())
    expires_at = now + SESSION_TTL_SECONDS
    payload = {
        "iss": settings.jwt_issuer,
        "aud": UI_AUDIENCE,
        "iat": now,
        "exp": expires_at,
        # 128 random bits, hex: the spec's "jti login'de 128 bit rastgele".
        "jti": secrets.token_hex(16),
        "pwf": _password_fingerprint(settings.ui_password),
    }
    encoded = jwt.encode(
        payload,
        settings.signing_key_bytes(),
        algorithm=ALGORITHM,
        headers={"kid": settings.jwt_kid},
    )
    return encoded, expires_at


def verify(settings: ApiSettings, token: str) -> UiClaims:
    try:
        header = jwt.get_unverified_header(token)
    except InvalidTokenError as exc:
        raise SessionInvalid("unreadable header") from exc
    if str(header.get("kid", "")) != settings.jwt_kid:
        raise SessionInvalid("unknown kid")

    try:
        payload = jwt.decode(
            token,
            settings.signing_key_bytes(),
            algorithms=[ALGORITHM],
            audience=UI_AUDIENCE,
            issuer=settings.jwt_issuer,
            leeway=LEEWAY_SECONDS,
            options={"require": list(REQUIRED_CLAIMS)},
        )
    except InvalidTokenError as exc:
        raise SessionInvalid("rejected") from exc

    expected_pwf = _password_fingerprint(settings.ui_password)
    if not hmac.compare_digest(str(payload["pwf"]), expected_pwf):
        raise SessionInvalid("password rotated")

    return UiClaims(jti=str(payload["jti"]), expires_at=int(payload["exp"]))
