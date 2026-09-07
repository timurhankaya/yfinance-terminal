"""The UI session cookie: a JWT with its own audience, so it is not an
access token and an access token is not a session."""

from __future__ import annotations

import time

import jwt as pyjwt
import pytest

from yfin.api.auth import jwt as access
from yfin.api.core.config import ApiSettings
from yfin.ui import session

KEY = "k" * 32
PW = "hunter2"


def settings() -> ApiSettings:
    return ApiSettings(
        _env_file=None, jwt_signing_key=KEY, jwt_kid="k1", jwt_issuer="yfin-api",
        ui_enabled=True, ui_password=PW,
    )


def test_issue_then_verify_round_trips() -> None:
    token, expires_at = session.issue(settings())
    claims = session.verify(settings(), token)
    assert claims.expires_at == expires_at
    assert len(claims.jti) == 32
    assert expires_at - int(time.time()) == pytest.approx(session.SESSION_TTL_SECONDS, abs=5)


def test_two_sessions_have_different_jti() -> None:
    a, _ = session.issue(settings())
    b, _ = session.issue(settings())
    assert session.verify(settings(), a).jti != session.verify(settings(), b).jti


def test_a_session_cookie_is_NOT_an_access_token() -> None:
    token, _ = session.issue(settings())
    with pytest.raises(access.TokenInvalid):
        access.verify(settings(), token)


def test_an_access_token_is_NOT_a_session_cookie() -> None:
    token, _ = access.mint(
        settings(), client_id="c1", scopes=("bars:read",), secret_id=1, epoch=0
    )
    with pytest.raises(session.SessionInvalid):
        session.verify(settings(), token)


def test_a_token_signed_with_another_key_is_rejected() -> None:
    other = ApiSettings(
        _env_file=None,
        jwt_signing_key="x" * 32,
        jwt_kid="k1",
        jwt_issuer="yfin-api",
    )
    token, _ = session.issue(other)
    with pytest.raises(session.SessionInvalid):
        session.verify(settings(), token)


def _raw(claims: dict[str, object]) -> str:
    return pyjwt.encode(
        claims,
        KEY.encode(),
        algorithm="HS256",
        headers={"kid": settings().jwt_kid},
    )


def test_an_expired_session_is_rejected() -> None:
    now = int(time.time())
    token = _raw(
        {"iss": settings().jwt_issuer, "aud": session.UI_AUDIENCE, "iat": now - 100_000,
         "exp": now - 90_000, "jti": "a" * 32}
    )
    with pytest.raises(session.SessionInvalid):
        session.verify(settings(), token)


def test_a_session_without_jti_is_rejected() -> None:
    now = int(time.time())
    token = _raw({
        "iss": settings().jwt_issuer,
        "aud": session.UI_AUDIENCE,
        "iat": now,
        "exp": now + 100,
    })
    with pytest.raises(session.SessionInvalid):
        session.verify(settings(), token)
