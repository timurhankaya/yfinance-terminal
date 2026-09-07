"""Serving the SPA: index.html under /ui, /ui/t/* and /ui/m/*, assets under
/ui/assets, nothing when the build output is absent, and never for an
API path."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from yfin.api.core.config import ApiSettings
from yfin.ui import pages

KEY = "k" * 32


def settings(*, ui_enabled: bool = True) -> ApiSettings:
    return ApiSettings(
        _env_file=None,
        jwt_signing_key=KEY,
        jwt_kid="k1",
        jwt_issuer="yfin-api",
        ui_enabled=ui_enabled,
        docs_enabled=True,
    )


def build_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>yfin</title><div id=root></div>")
    (dist / "assets" / "app.js").write_text("console.log('hi')")
    return dist


def make_client(dist: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from yfin.api.app import create_app

    # The only way in: create_app with the UI on, pointed at a temp build.
    monkeypatch.setattr(pages, "default_dist_dir", lambda: dist)
    return TestClient(create_app(settings()))


def test_index_is_served_at_ui_and_under_ui_t(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(build_dist(tmp_path), monkeypatch)
    for path in ("/ui", "/ui/", "/ui/t/AAPL/DES", "/ui/t/-/FA", "/ui/m/EQS", "/ui/m/WLA"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("text/html")
        assert "id=root" in response.text


def test_index_carries_the_csp_and_frame_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = make_client(build_dist(tmp_path), monkeypatch).get("/ui")
    assert response.headers["Content-Security-Policy"] == pages.CSP
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Cache-Control"] == "no-store"
    # Set by the API's SecurityHeadersMiddleware for every response, the
    # page included: an outbound article or EDGAR link must not carry the
    # terminal's URL (and with it the symbol being looked at) as a referrer.
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_assets_are_served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    response = make_client(build_dist(tmp_path), monkeypatch).get("/ui/assets/app.js")
    assert response.status_code == 200
    assert "console.log" in response.text


def test_a_missing_asset_is_404_not_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    response = make_client(build_dist(tmp_path), monkeypatch).get("/ui/assets/nope.js")
    assert response.status_code == 404


def test_ui_api_is_never_answered_with_html(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a build present the SPA fallback exists, so this is the case
    that matters: an unknown API path must be the mount's problem
    document, not index.html, for every method the browser can send."""
    client = make_client(build_dist(tmp_path), monkeypatch)
    for method in ("GET", "HEAD"):
        response = client.request(method, "/ui/api/no-such-thing", headers={"Accept": "text/html"})
        assert response.status_code == 404, method
        assert response.headers["content-type"] == "application/problem+json", method
        assert "id=root" not in response.text, method
    body = client.get("/ui/api/no-such-thing", headers={"Accept": "text/html"}).json()
    assert body["type"] == "not_found"


def test_v1_404s_are_untouched_by_the_spa(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    response = make_client(build_dist(tmp_path), monkeypatch).get(
        "/v1/typo", headers={"Accept": "text/html"}
    )
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"


def test_without_a_build_only_the_api_routes_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(tmp_path / "absent", monkeypatch)
    assert client.get("/ui").status_code == 404
    assert client.get("/ui/api/v1/datasets").status_code == 200


def test_index_without_an_assets_dir_is_treated_as_no_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<div id=root></div>")
    client = make_client(dist, monkeypatch)  # must not raise at startup
    assert client.get("/ui").status_code == 404


def test_with_the_ui_off_yfin_ui_is_never_imported() -> None:
    """This process has already imported yfin.ui (this file does), so the
    claim can only be checked in a fresh interpreter."""
    import subprocess
    import sys

    code = (
        "import sys; from yfin.api.core.config import ApiSettings; "
        "from yfin.api.app import create_app; "
        "create_app(ApiSettings(_env_file=None, jwt_signing_key='k' * 32)); "
        "assert 'yfin.ui' not in sys.modules, 'yfin.ui was imported with the UI off'"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_create_app_installs_the_ui_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(build_dist(tmp_path), monkeypatch)
    assert client.get("/ui").status_code == 200
    assert client.get("/ui/api/v1/datasets").status_code == 200


def test_create_app_leaves_ui_out_when_disabled() -> None:
    from yfin.api.app import create_app

    client = TestClient(create_app(settings(ui_enabled=False)))
    assert client.get("/ui/api/v1/datasets").status_code == 404
    assert client.get("/ui").status_code == 404


def test_the_openapi_document_has_no_ui_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from yfin.api.app import create_app

    monkeypatch.setattr(pages, "default_dist_dir", lambda: build_dist(tmp_path))
    paths = create_app(settings()).openapi()["paths"]
    assert not any(p.startswith("/ui") for p in paths)
