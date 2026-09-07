"""The public terminal: no login, the /v1 mirror under /ui/api/v1, and the
brake on it. /v1 itself must stay gated and its document unchanged."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from yfin.api.core.config import ApiSettings
from yfin.ui import public, session
from yfin.ui.session import COOKIE_NAME

KEY = "k" * 32
PW = "hunter2"


def settings(**overrides: object) -> ApiSettings:
    fields: dict[str, object] = {
        "_env_file": None,
        "jwt_signing_key": KEY,
        "jwt_kid": "k1",
        "jwt_issuer": "yfin-api",
        "ui_enabled": True,
        "ui_public": True,
        "ui_password": "",
    }
    fields.update(overrides)
    return ApiSettings(**fields)  # type: ignore[arg-type]


def make_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: object) -> TestClient:
    from yfin.api.app import create_app
    from yfin.ui import pages

    monkeypatch.setattr(pages, "default_dist_dir", lambda: tmp_path / "absent")
    return TestClient(create_app(settings(**overrides)))


def test_me_reports_a_public_terminal_as_signed_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = make_client(tmp_path, monkeypatch).get("/ui/api/me").json()
    assert body == {
        "authenticated": True,
        "expires_at": None,
        "live_enabled": False,
        "public": True,
    }


def test_login_and_logout_do_not_exist_in_public_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(tmp_path, monkeypatch)
    assert client.post("/ui/api/login", data={"password": "x"}).status_code == 404
    assert client.post("/ui/api/logout").status_code == 404


def test_the_mirror_serves_v1_reads_without_a_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The catalogue needs no database, so it proves routing and the
    principal override end to end."""
    client = make_client(tmp_path, monkeypatch)
    response = client.get("/ui/api/v1/datasets")
    assert response.status_code == 200
    assert any(entry["name"] == "news" for entry in response.json()["data"])
    # A validation failure on a real route shows the auth gate is gone here:
    # 422 for a one-letter prefix, not 401.
    assert client.get("/ui/api/v1/symbols", params={"q": "A"}).status_code == 422


def test_v1_itself_stays_gated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(tmp_path, monkeypatch)
    response = client.get("/v1/datasets")
    assert response.status_code == 401
    assert response.json()["type"] == "unauthenticated"


def test_the_mirror_is_not_in_the_openapi_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = make_client(tmp_path, monkeypatch).app.openapi()["paths"]  # type: ignore[attr-defined]
    assert "/v1/datasets" in paths
    assert not any(p.startswith("/ui") for p in paths)


def test_ui_only_routes_need_no_cookie_in_public_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """limit=0 fails validation before any database work, so the 422 shows
    the cookie gate was passed."""
    response = make_client(tmp_path, monkeypatch).get(
        "/ui/api/symbols/AAPL/news", params={"limit": 0}
    )
    assert response.status_code == 422


def test_the_brake_answers_429_per_address(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(tmp_path, monkeypatch, ui_requests_per_minute=2)
    assert client.get("/ui/api/v1/datasets").status_code == 200
    assert client.get("/ui/api/v1/datasets").status_code == 200
    limited = client.get("/ui/api/v1/datasets")
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "60"
    assert limited.json()["type"] == "rate_limit_exceeded"
    # The brake is the mirror's, not /v1's.
    assert client.get("/v1/datasets").status_code == 401


def test_password_mode_keeps_the_mirror_behind_the_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(tmp_path, monkeypatch, ui_public=False, ui_password=PW)
    assert client.get("/ui/api/v1/datasets").status_code == 401
    assert (
        client.get("/ui/api/v1/datasets", headers={"Authorization": "Bearer x"}).status_code == 401
    )
    token, _ = session.issue(settings(ui_public=False, ui_password=PW))
    client.cookies.set(COOKIE_NAME, token)
    assert client.get("/ui/api/v1/datasets").status_code == 200
    assert client.get("/ui/api/me").json()["public"] is False


def test_the_public_principal_is_the_ui_identity() -> None:
    assert public.PUBLIC_PRINCIPAL.client_id == "ui"
    assert "reference:read" in public.PUBLIC_PRINCIPAL.scopes
