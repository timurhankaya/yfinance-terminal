"""Token minting, verification, the token endpoint and the Bearer guard."""

from __future__ import annotations

import base64
import time
from collections.abc import Iterator
from typing import Annotated, Any

import fakeredis
import jwt as pyjwt
import pytest
from fastapi import FastAPI, Security
from fastapi.testclient import TestClient

from yfin.api.app import create_app
from yfin.api.auth import dependencies as deps
from yfin.api.auth import jwt as tokens
from yfin.api.auth.hashing import Candidate, hash_secret, new_secret
from yfin.api.core.config import ApiSettings
from yfin.api.core.errors import ApiProblem, install_error_handlers
from yfin.api.ratelimit import token_endpoint as limiter
from yfin.api.ratelimit.fixed_window import FixedWindow
from yfin.api.ratelimit.revocation import disabled_key, epoch_key, revoked_secret_key
from yfin.api.routers import oauth
from yfin.api.storage import clients as repo
from yfin.api.storage.session import session_scope

SIGNING_KEY = "k" * 48
CLIENT_ID = "yfc_" + "a" * 32
SCOPES = ("bars:read", "reference:read")


def settings(**overrides: Any) -> ApiSettings:
    base = {
        "jwt_signing_key": SIGNING_KEY,
        "jwt_kid": "k1",
        "jwt_issuer": "yfin-api",
        "jwt_audience": "yfin-api",
    }
    return ApiSettings(**(base | overrides))


@pytest.fixture
def redis() -> Iterator[fakeredis.FakeRedis]:
    server = fakeredis.FakeRedis(decode_responses=True)
    yield server
    server.flushall()


@pytest.fixture(autouse=True)
def _wire_redis(monkeypatch: pytest.MonkeyPatch, redis: fakeredis.FakeRedis) -> None:
    monkeypatch.setattr(deps, "get_redis", lambda _s: redis)
    monkeypatch.setattr(limiter, "get_redis", lambda _s: redis)
    monkeypatch.setattr(limiter, "_fallback", FixedWindow(limiter.WINDOW_SECONDS))


# --- JWT --------------------------------------------------------------------


def _mint(**overrides: Any) -> str:
    token, _ = tokens.mint(
        settings(),
        client_id=overrides.get("client_id", CLIENT_ID),
        scopes=overrides.get("scopes", SCOPES),
        secret_id=overrides.get("secret_id", 7),
        epoch=overrides.get("epoch", 3),
    )
    return token


def test_round_trip() -> None:
    claims = tokens.verify(settings(), _mint())
    assert claims.client_id == CLIENT_ID
    assert claims.scopes == SCOPES
    assert claims.secret_id == 7
    assert claims.epoch == 3


def test_wrong_audience_is_REJECTED() -> None:
    """Staging and production sharing a signing key is a routine accident;
    without an audience check a staging token would work in production."""
    with pytest.raises(tokens.TokenInvalid):
        tokens.verify(settings(jwt_audience="baska-api"), _mint())


def test_wrong_issuer_is_REJECTED() -> None:
    with pytest.raises(tokens.TokenInvalid):
        tokens.verify(settings(jwt_issuer="baska-issuer"), _mint())


def test_unknown_kid_is_REJECTED() -> None:
    with pytest.raises(tokens.TokenInvalid):
        tokens.verify(settings(jwt_kid="k2"), _mint())


def test_alg_none_is_REJECTED() -> None:
    """The classic forgery: an unsigned token that claims it needs no
    signature."""
    forged = pyjwt.encode(
        {"iss": "yfin-api", "aud": "yfin-api", "sub": CLIENT_ID, "iat": 0, "exp": 1 << 40,
         "jti": "x", "scope": "bars:read", "sid": 1, "epc": 0},
        key="",
        algorithm="none",
        headers={"kid": "k1"},
    )
    with pytest.raises(tokens.TokenInvalid):
        tokens.verify(settings(), forged)


def test_token_signed_with_another_key_is_REJECTED() -> None:
    forged = pyjwt.encode(
        {"iss": "yfin-api", "aud": "yfin-api", "sub": CLIENT_ID, "iat": int(time.time()),
         "exp": int(time.time()) + 60, "jti": "x", "scope": "bars:read", "sid": 1, "epc": 0},
        key="z" * 48,
        algorithm="HS256",
        headers={"kid": "k1"},
    )
    with pytest.raises(tokens.TokenInvalid):
        tokens.verify(settings(), forged)


def test_expired_token_is_REJECTED() -> None:
    past = int(time.time()) - 3600
    expired = pyjwt.encode(
        {"iss": "yfin-api", "aud": "yfin-api", "sub": CLIENT_ID, "iat": past,
         "exp": past + 60, "jti": "x", "scope": "bars:read", "sid": 1, "epc": 0},
        key=SIGNING_KEY,
        algorithm="HS256",
        headers={"kid": "k1"},
    )
    with pytest.raises(tokens.TokenInvalid):
        tokens.verify(settings(), expired)


@pytest.mark.parametrize("missing", ["sid", "epc", "jti", "iss", "aud"])
def test_missing_required_claim_is_REJECTED(missing: str) -> None:
    payload = {
        "iss": "yfin-api", "aud": "yfin-api", "sub": CLIENT_ID,
        "iat": int(time.time()), "exp": int(time.time()) + 60,
        "jti": "x", "scope": "bars:read", "sid": 1, "epc": 0,
    }
    del payload[missing]
    token = pyjwt.encode(payload, key=SIGNING_KEY, algorithm="HS256", headers={"kid": "k1"})
    with pytest.raises(tokens.TokenInvalid):
        tokens.verify(settings(), token)


# --- the token endpoint -----------------------------------------------------


def _basic(client_id: str, credential: str) -> str:
    raw = f"{client_id}:{credential}".encode()
    return "Basic " + base64.b64encode(raw).decode()


@pytest.fixture
def credential() -> str:
    return new_secret()


@pytest.fixture
def record(credential: str) -> repo.AuthRecord:
    return repo.AuthRecord(
        client_id=CLIENT_ID,
        is_active=True,
        auth_epoch=3,
        scopes=SCOPES,
        candidates=[Candidate(secret_id=7, secret_hash=hash_secret(credential), is_usable=True)],
    )


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch, record: repo.AuthRecord
) -> Iterator[TestClient]:
    app = create_app(settings())
    app.dependency_overrides[session_scope] = lambda: None
    monkeypatch.setattr(
        oauth.repo, "load_for_auth", lambda _s, cid: record if cid == CLIENT_ID else None
    )
    with TestClient(app) as test_client:
        yield test_client


def _token_request(client: TestClient, auth: str | None = None, **form: str):  # type: ignore[no-untyped-def]
    headers = {"Authorization": auth} if auth else {}
    return client.post(
        "/oauth/token", data={"grant_type": "client_credentials"} | form, headers=headers
    )


def test_token_is_issued(client: TestClient, credential: str) -> None:
    response = _token_request(client, _basic(CLIENT_ID, credential))
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["scope"] == " ".join(SCOPES)
    assert tokens.verify(settings(), body["access_token"]).client_id == CLIENT_ID


def test_token_response_is_NEVER_CACHED(client: TestClient, credential: str) -> None:
    """RFC 6749 §5.1: the response carries a bearer credential."""
    response = _token_request(client, _basic(CLIENT_ID, credential))
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"


def test_error_body_follows_RFC_6749(client: TestClient) -> None:
    """Not problem+json. OAuth2 client libraries look for `error`, and a
    contract test would not catch the difference: a problem document would
    still match the schema we published."""
    response = _token_request(client, _basic(CLIENT_ID, "wrong"))
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")
    assert set(response.json()) == {"error", "error_description"}
    assert response.json()["error"] == "invalid_client"


def test_401_carries_WWW_Authenticate_Basic(client: TestClient) -> None:
    response = _token_request(client, _basic(CLIENT_ID, "wrong"))
    assert response.headers["WWW-Authenticate"].startswith("Basic realm=")


def test_unknown_client_and_wrong_credential_answer_IDENTICALLY(
    client: TestClient,
) -> None:
    """The body must not answer what the constant-time verification path
    exists to keep unanswered."""
    unknown = _token_request(client, _basic("yfc_" + "z" * 32, "x"))
    wrong = _token_request(client, _basic(CLIENT_ID, "wrong"))
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


def test_disabled_client_answers_IDENTICALLY_too(
    client: TestClient, credential: str, record: repo.AuthRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    disabled = repo.AuthRecord(
        client_id=record.client_id,
        is_active=False,
        auth_epoch=record.auth_epoch,
        scopes=record.scopes,
        candidates=record.candidates,
    )
    monkeypatch.setattr(oauth.repo, "load_for_auth", lambda _s, _cid: disabled)
    response = _token_request(client, _basic(CLIENT_ID, credential))
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_client"


def test_credential_in_the_body_is_REJECTED(client: TestClient, credential: str) -> None:
    """client_secret_post is unsupported; saying so beats a generic
    failure the caller would read as "wrong credential"."""
    response = _token_request(client, None, client_secret=credential)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_missing_basic_header_is_400(client: TestClient) -> None:
    response = _token_request(client)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"
    assert response.headers["WWW-Authenticate"].startswith("Basic realm=")


def test_unsupported_grant_type(client: TestClient, credential: str) -> None:
    response = client.post(
        "/oauth/token",
        data={"grant_type": "password"},
        headers={"Authorization": _basic(CLIENT_ID, credential)},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "unsupported_grant_type"


def test_percent_encoded_basic_is_DECODED(
    monkeypatch: pytest.MonkeyPatch, record: repo.AuthRecord
) -> None:
    """RFC 6749 §2.3.1 wants the two halves form-urlencoded before base64.
    A spec-following client would otherwise fail for no visible reason."""
    raw_credential = "a b+c"
    stored = repo.AuthRecord(
        client_id=CLIENT_ID,
        is_active=True,
        auth_epoch=1,
        scopes=SCOPES,
        candidates=[
            Candidate(secret_id=1, secret_hash=hash_secret(raw_credential), is_usable=True)
        ],
    )
    app = create_app(settings())
    app.dependency_overrides[session_scope] = lambda: None
    monkeypatch.setattr(oauth.repo, "load_for_auth", lambda _s, _cid: stored)

    encoded = base64.b64encode(f"{CLIENT_ID}:a+b%2Bc".encode()).decode()
    with TestClient(app) as test_client:
        response = test_client.post(
            "/oauth/token",
            data={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {encoded}"},
        )
    assert response.status_code == 200


def test_a_narrower_scope_can_be_requested(client: TestClient, credential: str) -> None:
    response = _token_request(client, _basic(CLIENT_ID, credential), scope="bars:read")
    assert response.status_code == 200
    assert response.json()["scope"] == "bars:read"
    assert tokens.verify(settings(), response.json()["access_token"]).scopes == ("bars:read",)


def test_a_scope_not_granted_CANNOT_be_requested(client: TestClient, credential: str) -> None:
    response = _token_request(client, _basic(CLIENT_ID, credential), scope="news:read")
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_scope"


def test_exceeding_the_IP_limit_is_429(client: TestClient, credential: str) -> None:
    for _ in range(limiter.IP_LIMIT):
        _token_request(client, _basic(CLIENT_ID, credential))
    response = _token_request(client, _basic(CLIENT_ID, credential))
    assert response.status_code == 429
    assert response.headers["Retry-After"]


def test_token_endpoint_FAILS_CLOSED_without_redis(
    client: TestClient, credential: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one fail-closed path in the API: losing Redis here does not
    merely stop counting, it removes the only brake on an endpoint that
    runs an argon2 verification for anyone who asks."""
    def broken(_settings: ApiSettings) -> Any:
        raise ConnectionError("redis is down")

    monkeypatch.setattr(limiter, "get_redis", broken)
    monkeypatch.setattr(limiter, "_fallback", FixedWindow(limiter.WINDOW_SECONDS))

    allowed = 0
    for _ in range(limiter.FALLBACK_IP_LIMIT + 3):
        if _token_request(client, _basic(CLIENT_ID, credential)).status_code == 200:
            allowed += 1
    assert allowed == limiter.FALLBACK_IP_LIMIT

    response = _token_request(client, _basic(CLIENT_ID, credential))
    assert response.status_code == 503
    assert response.json()["error"] == "temporarily_unavailable"


# --- the Bearer guard -------------------------------------------------------


#: The pattern real endpoints use: the scope is declared once, inside
#: Annotated, and that single declaration both lands in the OpenAPI
#: document and is what gets enforced.
NeedsBars = Annotated[deps.Principal, Security(deps.current_principal, scopes=["bars:read"])]
NeedsNews = Annotated[deps.Principal, Security(deps.current_principal, scopes=["news:read"])]


def _guarded_app() -> FastAPI:
    app = create_app(settings())

    @app.get("/protected")
    def protected(principal: NeedsBars) -> dict[str, str]:
        return {"client_id": principal.client_id}

    @app.get("/protected/{item}")
    def protected_item(item: str, principal: NeedsNews) -> dict[str, str]:
        raise ApiProblem(404, "not_found", "no such item")

    install_error_handlers(app)
    return app


@pytest.fixture
def guarded() -> Iterator[TestClient]:
    with TestClient(_guarded_app()) as client:
        yield client


def test_missing_token_is_401_with_NO_error_parameter(guarded: TestClient) -> None:
    """RFC 6750 §3 reserves `error` for a request that presented
    something; a bare challenge is what a missing credential gets."""
    response = guarded.get("/protected")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == 'Bearer realm="yfin-api"'


def test_malformed_token_is_401_invalid_token(guarded: TestClient) -> None:
    response = guarded.get("/protected", headers={"Authorization": "Bearer bozuk"})
    assert response.status_code == 401
    assert 'error="invalid_token"' in response.headers["WWW-Authenticate"]


def test_a_valid_token_passes(guarded: TestClient) -> None:
    response = guarded.get("/protected", headers={"Authorization": f"Bearer {_mint()}"})
    assert response.status_code == 200
    assert response.json()["client_id"] == CLIENT_ID


def test_missing_scope_is_403_and_names_the_required_scope(guarded: TestClient) -> None:
    token = _mint(scopes=("reference:read",))
    response = guarded.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    assert response.json()["type"] == "insufficient_scope"
    assert 'scope="bars:read"' in response.headers["WWW-Authenticate"]


def test_scope_is_checked_BEFORE_existence(guarded: TestClient) -> None:
    """Otherwise a caller enumerates what exists by reading 404 against
    403."""
    token = _mint(scopes=("bars:read",))
    response = guarded.get("/protected/yok", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


def test_token_of_a_disabled_client_is_REJECTED(
    guarded: TestClient, redis: fakeredis.FakeRedis
) -> None:
    redis.set(disabled_key(CLIENT_ID), "1")
    response = guarded.get("/protected", headers={"Authorization": f"Bearer {_mint()}"})
    assert response.status_code == 401
    assert response.json()["type"] == "client_disabled"


def test_stale_epoch_is_REJECTED(guarded: TestClient, redis: fakeredis.FakeRedis) -> None:
    """A narrowed scope or a downgraded plan bites now, not at expiry."""
    redis.set(epoch_key(CLIENT_ID), "9")
    response = guarded.get("/protected", headers={"Authorization": f"Bearer {_mint(epoch=3)}"})
    assert response.status_code == 401


def test_current_epoch_is_ACCEPTED(guarded: TestClient, redis: fakeredis.FakeRedis) -> None:
    redis.set(epoch_key(CLIENT_ID), "3")
    response = guarded.get("/protected", headers={"Authorization": f"Bearer {_mint(epoch=3)}"})
    assert response.status_code == 200


def test_revoking_a_credential_KILLS_its_tokens_too(
    guarded: TestClient, redis: fakeredis.FakeRedis
) -> None:
    """Revoking a leaked credential must kill the tokens already minted
    from it, not just future authentications."""
    redis.set(revoked_secret_key(7), "1")
    response = guarded.get("/protected", headers={"Authorization": f"Bearer {_mint(secret_id=7)}"})
    assert response.status_code == 401


def test_openapi_scope_comes_from_the_SAME_source_as_the_enforced_one(
    guarded: TestClient,
) -> None:
    """The Authorize button in /docs works off this, and -- more
    importantly -- the document cannot advertise one scope while the code
    checks another, because both come from the same Security() call."""
    document = guarded.get("/openapi.json").json()
    scheme = document["components"]["securitySchemes"]["clientCredentials"]
    flow = scheme["flows"]["clientCredentials"]
    assert flow["tokenUrl"] == "/oauth/token"
    assert "bars:read" in flow["scopes"]

    declared = document["paths"]["/protected"]["get"]["security"]
    assert declared == [{"clientCredentials": ["bars:read"]}]


def test_read_path_FAILS_OPEN_without_redis(
    guarded: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opposite choice from the token endpoint, on purpose: refusing all
    traffic because a counter store is down would be a larger outage than
    the window it protects, and that window is one token lifetime."""
    def broken(_settings: ApiSettings) -> Any:
        raise ConnectionError("redis is down")

    monkeypatch.setattr(deps, "get_redis", broken)
    response = guarded.get("/protected", headers={"Authorization": f"Bearer {_mint()}"})
    assert response.status_code == 200


# --- the Redis identity handshake -------------------------------------------


def test_publishing_into_an_UNCLAIMED_redis_is_refused(
    redis: fakeredis.FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`yfin api client disable` once wrote a revocation to
    the operator's local Redis while the API read a different one. A Redis
    answered, so the command reported success -- and the disabled client
    kept serving. An exit code that proves only "some Redis answered" is
    the false assurance this design set out to avoid."""
    from yfin.api.ratelimit import revocation

    monkeypatch.setattr(revocation, "get_redis", lambda _s: redis)
    with pytest.raises(revocation.WrongRedis):
        revocation.publish_revocation(settings(), CLIENT_ID, epoch=1, disabled=True)


def test_publishing_works_once_an_api_has_claimed_it(
    redis: fakeredis.FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    from yfin.api.ratelimit import revocation

    monkeypatch.setattr(revocation, "get_redis", lambda _s: redis)
    revocation.mark_api_redis(settings())
    revocation.publish_revocation(settings(), CLIENT_ID, epoch=1, disabled=True)
    assert redis.get(revocation.disabled_key(CLIENT_ID)) == "1"


def test_the_marker_survives_the_api_being_down(
    redis: fakeredis.FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A revocation is most likely to be issued while something is wrong,
    so the marker must not expire with the process that wrote it."""
    from yfin.api.ratelimit import revocation

    monkeypatch.setattr(revocation, "get_redis", lambda _s: redis)
    revocation.mark_api_redis(settings())
    assert redis.ttl(revocation.MARKER_KEY) == -1
