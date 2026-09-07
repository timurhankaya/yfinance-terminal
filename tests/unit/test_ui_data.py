"""UI-only data routes: parameter validation, no database."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from yfin.api.core.config import ApiSettings

KEY = "k" * 32


def settings() -> ApiSettings:
    return ApiSettings(
        _env_file=None,
        jwt_signing_key=KEY,
        jwt_kid="k1",
        jwt_issuer="yfin-api",
        ui_enabled=True,
    )


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from yfin.api.app import create_app
    from yfin.ui import pages

    monkeypatch.setattr(pages, "default_dist_dir", lambda: tmp_path / "absent")
    return TestClient(create_app(settings()))


def test_news_limit_above_the_cap_is_422(client: TestClient) -> None:
    """The cap is checked before the query, so a bad limit never reaches a
    database."""
    response = client.get("/ui/api/symbols/AAPL/news", params={"limit": 201})
    assert response.status_code == 422
    assert response.json()["type"] == "invalid_parameter"


def test_news_limit_zero_is_422(client: TestClient) -> None:
    response = client.get("/ui/api/symbols/AAPL/news", params={"limit": 0})
    assert response.status_code == 422
    assert response.json()["type"] == "invalid_parameter"


def test_the_route_is_not_in_the_schema(client: TestClient) -> None:
    paths = client.app.openapi()["paths"]  # type: ignore[attr-defined]
    assert not any(p.startswith("/ui") for p in paths)
