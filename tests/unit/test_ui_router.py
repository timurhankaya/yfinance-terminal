"""Login, logout, me and the /ui/api catch-all."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from yfin.api.core.config import ApiSettings
from yfin.api.core.errors import install_error_handlers
from yfin.api.core.middleware import RequestContextMiddleware
from yfin.api.ratelimit.fixed_window import FixedWindow
from yfin.ui import router as ui_router
from yfin.ui.session import COOKIE_NAME

KEY = "k" * 32
PW = "hunter2"
WRONG_PW = "hunter3"
FIELD = ui_router.LOGIN_FIELD


def make_client(monkeypatch: pytest.MonkeyPatch, *, public_base_url: str = "") -> TestClient:
    settings = ApiSettings(
        _env_file=None,
        jwt_signing_key=KEY,
        jwt_kid="k1",
        jwt_issuer="yfin-api",
        ui_enabled=True,
        ui_public=False,
        ui_password=PW,
        public_base_url=public_base_url,
    )
    app = FastAPI()
    app.state.api_settings = settings
    install_error_handlers(app)
    app.add_middleware(RequestContextMiddleware, settings=settings)
    app.include_router(ui_router.router)

    # A throwaway route so the UiSession dependency is exercised in 1a,
    # before any real UI-only data route (1c, 1d) uses it.
    @app.get("/probe", include_in_schema=False)
    def probe(claims: ui_router.UiSession) -> dict[str, str]:
        return {"jti": claims.jti}

    monkeypatch.setattr(ui_router, "_login_limiter", FixedWindow())
    return TestClient(app)


def do_login(client: TestClient, value: str = PW):  # type: ignore[no-untyped-def]
    return client.post("/ui/api/login", data={FIELD: value})


def test_me_without_a_cookie_is_200_and_unauthenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    response = make_client(monkeypatch).get("/ui/api/me")
    assert response.status_code == 200
    assert response.json() == {
        "authenticated": False,
        "expires_at": None,
        "live_enabled": False,
        "public": False,
    }


def test_login_sets_an_httponly_lax_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    response = do_login(make_client(monkeypatch))
    assert response.status_code == 204
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE_NAME}=")
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/" in cookie
    assert "Secure" not in cookie


def test_the_cookie_is_secure_behind_https(monkeypatch: pytest.MonkeyPatch) -> None:
    response = do_login(make_client(monkeypatch, public_base_url="https://yfin.example"))
    assert "Secure" in response.headers["set-cookie"]


def test_me_after_login_is_authenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(monkeypatch)
    do_login(client)
    body = client.get("/ui/api/me").json()
    assert body["authenticated"] is True
    assert body["expires_at"] is not None


def test_a_wrong_password_is_401_without_a_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    response = do_login(make_client(monkeypatch), WRONG_PW)
    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "unauthenticated"
    assert "set-cookie" not in response.headers


def test_login_is_limited_per_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(monkeypatch)
    for _ in range(ui_router.LOGIN_ATTEMPTS_PER_MINUTE):
        assert do_login(client, WRONG_PW).status_code == 401
    limited = do_login(client)
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "60"
    assert limited.json()["type"] == "rate_limit_exceeded"


def test_logout_clears_the_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(monkeypatch)
    do_login(client)
    response = client.post("/ui/api/logout")
    assert response.status_code == 204
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert client.get("/ui/api/me").json()["authenticated"] is False


def test_a_tampered_cookie_is_unauthenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(monkeypatch)
    client.cookies.set(COOKIE_NAME, "not.a.jwt")
    assert client.get("/ui/api/me").json()["authenticated"] is False


def test_unknown_ui_api_paths_are_404_problems(monkeypatch: pytest.MonkeyPatch) -> None:
    response = make_client(monkeypatch).get(
        "/ui/api/no-such-thing", headers={"Accept": "text/html"}
    )
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "not_found"


def test_nothing_under_ui_api_is_in_the_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(monkeypatch)
    paths = client.app.openapi()["paths"]  # type: ignore[attr-defined]
    assert not any(p.startswith("/ui") for p in paths)


def test_head_on_an_unknown_ui_api_path_is_404_too(monkeypatch: pytest.MonkeyPatch) -> None:
    assert make_client(monkeypatch).head("/ui/api/nope").status_code == 404


def test_ui_session_dependency_without_a_cookie_is_401_unauthenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = make_client(monkeypatch).get("/probe")
    assert response.status_code == 401
    assert response.json()["type"] == "unauthenticated"


def test_ui_session_dependency_with_a_bad_cookie_is_401_invalid_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(monkeypatch)
    client.cookies.set(COOKIE_NAME, "not.a.jwt")
    response = client.get("/probe")
    assert response.status_code == 401
    assert response.json()["type"] == "invalid_token"


def test_ui_session_dependency_IGNORES_a_bearer_header(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(monkeypatch)
    response = client.get("/probe", headers={"Authorization": "Bearer anything"})
    assert response.status_code == 401


def test_ui_session_dependency_accepts_the_login_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(monkeypatch)
    do_login(client)
    response = client.get("/probe")
    assert response.status_code == 200
    assert len(response.json()["jti"]) == 32
