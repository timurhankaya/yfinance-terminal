"""A session cookie reaches /v1 as the `ui` principal, with every read
scope and no metering at all."""

from __future__ import annotations

from typing import Annotated

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from yfin.api.auth.dependencies import UI_CLIENT_ID, UI_PAGE_CAP, Authenticated, Principal
from yfin.api.core.config import ApiSettings
from yfin.api.core.errors import install_error_handlers
from yfin.api.ratelimit import policy, usage
from yfin.api.ratelimit.dependencies import UsageMiddleware, guard
from yfin.core.families import DataFamily, scope_for
from yfin.ui import session
from yfin.ui.session import COOKIE_NAME

KEY = "k" * 32
PW = "hunter2"


def settings(*, ui_enabled: bool = True) -> ApiSettings:
    return ApiSettings(
        _env_file=None, jwt_signing_key=KEY, jwt_kid="k1", jwt_issuer="yfin-api",
        ui_enabled=ui_enabled, ui_password=PW,
    )


def make_client(monkeypatch: pytest.MonkeyPatch, *, ui_enabled: bool = True) -> TestClient:
    # `ratelimit/dependencies.py` calls `policy.limits_for_client(...)` and
    # `usage.record(...)` through the module objects, so patching the
    # module attributes is what the code under test sees.
    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("the UI must never be metered or recorded")

    monkeypatch.setattr(policy, "limits_for_client", explode)
    monkeypatch.setattr(usage, "record", explode)

    app = FastAPI()
    app.state.api_settings = settings(ui_enabled=ui_enabled)
    install_error_handlers(app)
    app.add_middleware(UsageMiddleware)

    @app.get("/who")
    def who(principal: Authenticated) -> dict[str, object]:
        return {"client_id": principal.client_id, "scopes": sorted(principal.scopes)}

    # Same shape as market.py: guard() goes through Depends(), never as a
    # bare default value (FastAPI would treat that as a body parameter).
    @app.get("/bars")
    def bars(
        request: Request,
        principal: Annotated[Principal, Depends(guard(DataFamily.BARS))],
    ) -> dict[str, object]:
        return {"cap": request.state.page_size_cap, "metered": hasattr(request.state, "limits")}

    return TestClient(app)


def with_cookie(client: TestClient) -> TestClient:
    token, _ = session.issue(settings())
    client.cookies.set(COOKIE_NAME, token)
    return client


def test_a_cookie_yields_the_ui_principal_with_every_read_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = with_cookie(make_client(monkeypatch)).get("/who").json()
    assert body["client_id"] == UI_CLIENT_ID
    assert body["scopes"] == sorted(scope_for(f) for f in DataFamily)


def test_a_cookie_passes_a_guarded_route_UNMETERED(monkeypatch: pytest.MonkeyPatch) -> None:
    response = with_cookie(make_client(monkeypatch)).get("/bars")
    assert response.status_code == 200
    assert response.json() == {"cap": UI_PAGE_CAP, "metered": False}


def test_no_credentials_is_still_401(monkeypatch: pytest.MonkeyPatch) -> None:
    response = make_client(monkeypatch).get("/who")
    assert response.status_code == 401
    assert response.json()["type"] == "unauthenticated"


def test_a_bad_cookie_is_401_invalid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(monkeypatch)
    client.cookies.set(COOKIE_NAME, "not.a.jwt")
    response = client.get("/who")
    assert response.status_code == 401
    assert response.json()["type"] == "invalid_token"


def test_the_cookie_is_IGNORED_when_the_ui_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(monkeypatch, ui_enabled=False)
    token, _ = session.issue(settings())
    client.cookies.set(COOKIE_NAME, token)
    assert client.get("/who").status_code == 401


def test_a_bearer_header_is_evaluated_ALONE_even_with_a_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Header present and invalid must not fall through to the cookie:
    a client that sends both would otherwise get the UI's scopes."""
    client = with_cookie(make_client(monkeypatch))
    response = client.get("/who", headers={"Authorization": "Bearer nonsense"})
    assert response.status_code == 401
    assert response.json()["type"] == "invalid_token"
