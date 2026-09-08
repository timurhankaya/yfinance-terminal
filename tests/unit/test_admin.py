"""The admin page: off without a secret, Basic auth with a brake on
misses, and each page rendering from the ops layer (mocked: no database
in unit tests)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from yfin.admin import auth, ops
from yfin.api.core.config import ApiSettings
from yfin.api.storage.session import session_scope
from yfin.storage.settings_store import SettingRejected

KEY = "k" * 32
PHRASE = "open-sesame-42"
AUTH = ("operator", PHRASE)
#: The settings field that switches the admin page on.
ADMIN_FIELD = "admin_" + "password"


def settings(**overrides: object) -> ApiSettings:
    fields: dict[str, object] = {"_env_file": None, "jwt_signing_key": KEY, ADMIN_FIELD: PHRASE}
    fields.update(overrides)
    return ApiSettings(**fields)  # type: ignore[arg-type]


def make_client(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> TestClient:
    from yfin.api.app import create_app

    auth._failures.reset()
    app = create_app(settings(**overrides))
    # No database in unit tests: the ops layer is mocked per test and the
    # session dependency yields nothing.
    app.dependency_overrides[session_scope] = lambda: None
    return TestClient(app)


def test_no_secret_means_no_admin_page(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(monkeypatch, **{ADMIN_FIELD: ""})
    assert client.get("/admin/settings", auth=AUTH).status_code == 404
    assert client.get("/admin").status_code == 404


def test_the_admin_page_is_not_in_the_openapi_document(monkeypatch: pytest.MonkeyPatch) -> None:
    paths = make_client(monkeypatch).app.openapi()["paths"]  # type: ignore[attr-defined]
    assert not any(p.startswith("/admin") for p in paths)


def test_without_credentials_the_browser_is_challenged(monkeypatch: pytest.MonkeyPatch) -> None:
    response = make_client(monkeypatch).get("/admin/settings")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == 'Basic realm="yfin admin"'
    assert response.json()["type"] == "unauthenticated"


def test_wrong_secret_is_401_and_five_misses_become_429(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(monkeypatch)
    for _ in range(auth.FAILURES_PER_MINUTE):
        assert client.get("/admin/settings", auth=("x", "wrong")).status_code == 401
    limited = client.get("/admin/settings", auth=("x", "wrong"))
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "60"
    # The brake counts attempts, so even the right secret waits out the window.
    assert client.get("/admin/settings", auth=AUTH).status_code == 429


def test_working_the_page_does_not_spend_the_login_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Five is failures per minute, not requests per minute. An operator
    reading four pages and saving a setting is one minute of ordinary
    work, and it must not end in a 429 on their own admin."""
    client = make_client(monkeypatch)
    for _ in range(auth.FAILURES_PER_MINUTE * 3):
        assert client.get("/admin/settings", auth=AUTH).status_code == 200
    # And the brake still works afterwards: the window was never touched.
    for _ in range(auth.FAILURES_PER_MINUTE):
        assert client.get("/admin/settings", auth=("x", "wrong")).status_code == 401
    assert client.get("/admin/settings", auth=("x", "wrong")).status_code == 429


class TestCrossSiteWrites:
    """Basic auth says WHO; nothing in a cross-site form POST says the
    operator asked for it. The browser attaches the cached credential
    automatically -- there is no cookie, so `SameSite` applies to
    nothing, and the CSP's `form-action 'self'` restricts where OUR
    forms may post, not where another origin's may post to us.

    The concrete write it stops: a page anywhere posting
    `url=http://attacker:secret@evil/` to /admin/proxies, after which
    `yfin sync` routes Yahoo traffic through a host the attacker owns
    and the operator sees nothing but their own settings page.
    """

    def _post(self, client: TestClient, **headers: str) -> Any:
        return client.post(
            "/admin/settings/yf_max_shards",
            data={"value": "6"},
            auth=AUTH,
            headers=headers,
            follow_redirects=False,
        )

    @pytest.fixture
    def client(self, monkeypatch: pytest.MonkeyPatch) -> TestClient:
        monkeypatch.setattr(ops, "set_setting", lambda key, value, *, settings: key.lower())
        monkeypatch.setattr(ops, "fetch_rows", lambda _settings: {})
        return make_client(monkeypatch)

    def test_a_cross_site_post_is_refused(self, client: TestClient) -> None:
        response = self._post(client, **{"sec-fetch-site": "cross-site"})
        assert response.status_code == 403
        assert response.json()["type"] == "cross_site_request"

    def test_a_same_site_post_is_refused_too(self, client: TestClient) -> None:
        """A sibling subdomain is not the admin page, and a subdomain is
        the easiest origin for an attacker to end up holding."""
        assert self._post(client, **{"sec-fetch-site": "same-site"}).status_code == 403

    def test_the_page_own_form_still_posts(self, client: TestClient) -> None:
        response = self._post(client, **{"sec-fetch-site": "same-origin"})
        assert response.status_code == 303

    def test_a_foreign_origin_is_refused_without_fetch_metadata(self, client: TestClient) -> None:
        """Older browsers send no `Sec-Fetch-Site`; `Origin` is then what
        there is, compared the way `/ui/ws` compares it."""
        response = self._post(client, origin="https://evil.example")
        assert response.status_code == 403

    def test_the_deployments_own_origin_is_accepted(self, client: TestClient) -> None:
        response = self._post(client, origin="http://testserver")
        assert response.status_code == 303

    def test_a_client_that_is_not_a_browser_is_untouched(self, client: TestClient) -> None:
        """`curl` and the operator's own scripts send neither header, and
        refusing them would break every one while stopping no attack: a
        program that sets its own headers is not what this defends
        against."""
        assert self._post(client).status_code == 303

    def test_reads_are_not_affected(self, client: TestClient) -> None:
        """A GET changes nothing, so nothing has to establish intent."""
        response = client.get(
            "/admin/settings", auth=AUTH, headers={"sec-fetch-site": "cross-site"}
        )
        assert response.status_code == 200

    def test_every_write_route_is_covered(self, client: TestClient) -> None:
        """The guard is in `require_admin`, the one dependency all four
        share, so a new write route cannot acquire auth and miss it."""
        cross_site = {"sec-fetch-site": "cross-site"}
        for path, payload in (
            ("/admin/settings/yf_max_shards", {"value": "6"}),
            ("/admin/settings/yf_max_shards/unset", {}),
            ("/admin/proxies", {"url": "http://h:1"}),
            ("/admin/proxies/7/remove", {}),
            ("/admin/screens/day_gainers/disable", {}),
        ):
            response = client.post(path, data=payload, auth=AUTH, headers=cross_site)
            assert response.status_code == 403, path

    def test_a_cross_site_post_does_not_spend_the_login_window(self, client: TestClient) -> None:
        """It is not a failed login, and counting it as one would let a
        cross-site page lock the operator out of their own admin."""
        for _ in range(auth.FAILURES_PER_MINUTE + 1):
            assert self._post(client, **{"sec-fetch-site": "cross-site"}).status_code == 403
        assert client.get("/admin/settings", auth=AUTH).status_code == 200


def test_root_redirects_to_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    response = make_client(monkeypatch).get("/admin", auth=AUTH, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/admin/settings"


def test_settings_page_shows_schema_state_and_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ops, "fetch_rows", lambda _settings: {"yf_max_shards": "4"})
    response = make_client(monkeypatch).get("/admin/settings", auth=AUTH)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["Content-Security-Policy"].startswith("default-src 'none'")
    assert response.headers["Referrer-Policy"] == "no-referrer"
    html = response.text
    assert "<code>yf_max_shards</code>" in html
    assert 'name=value value="4"' in html
    assert 'class="source-db"' in html
    assert 'action="/admin/settings/yf_max_shards/unset"' in html
    # A key with no row has no Unset button and reads env/default.
    assert 'action="/admin/settings/yf_domain_regions/unset"' not in html


def test_the_settings_page_groups_keys_and_names_what_was_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seventy keys in one flat table is a document, not a console. They
    are split by the group the schema already carries, and the ones that
    no longer match their default are named at the top -- that is the
    question an operator opens this page with."""
    monkeypatch.setattr(ops, "fetch_rows", lambda _settings: {"yf_max_shards": "4"})
    html = make_client(monkeypatch).get("/admin/settings", auth=AUTH).text

    # The group the key belongs to is a section with an anchor, and the
    # summary at the top links to the row itself.
    assert 'id="g-shard"' in html
    assert 'id="s-yf_max_shards"' in html
    assert '<a href="#s-yf_max_shards">yf_max_shards</a>' in html
    assert "settings differ from their defaults" in html

    # The changed row is marked as such; a default one is not.
    assert '<tr id="s-yf_max_shards" class="changed">' in html
    assert '<tr id="s-yf_domain_regions" class="">' in html

    # The source reads as a place, not as the storage layer's shorthand.
    assert ">database</span>" in html


def test_settings_save_goes_through_the_validated_store(monkeypatch: pytest.MonkeyPatch) -> None:
    written: list[tuple[str, str]] = []

    def fake_set(key: str, value: str, *, settings: Any) -> str:
        written.append((key, value))
        return key.lower()

    monkeypatch.setattr(ops, "set_setting", fake_set)
    client = make_client(monkeypatch)
    response = client.post(
        "/admin/settings/YF_MAX_SHARDS", data={"value": "6"}, auth=AUTH, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/settings?ok=yf_max_shards+saved"
    assert written == [("YF_MAX_SHARDS", "6")]


def test_a_rejected_setting_comes_back_as_an_error_flash(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(key: str, value: str, *, settings: Any) -> str:
        raise SettingRejected("yf_max_shards: Input should be greater than or equal to 1")

    monkeypatch.setattr(ops, "set_setting", refuse)
    monkeypatch.setattr(ops, "fetch_rows", lambda _settings: {})
    client = make_client(monkeypatch)
    response = client.post("/admin/settings/yf_max_shards", data={"value": "0"}, auth=AUTH)
    assert response.status_code == 200  # followed the redirect back to the page
    assert "greater than or equal to 1" in response.text
    assert 'class="flash error"' in response.text


def test_settings_unset_deletes_the_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ops, "unset_setting", lambda key, *, settings: True)
    response = make_client(monkeypatch).post(
        "/admin/settings/yf_max_shards/unset", auth=AUTH, follow_redirects=False
    )
    assert response.headers["location"] == "/admin/settings?ok=yf_max_shards+unset"


def fake_proxy(**overrides: Any) -> SimpleNamespace:
    from yfin.models import ProxyHealth, ProxyScheme

    base: dict[str, Any] = {
        "id": 7,
        "label": "p7",
        "scheme": ProxyScheme.HTTP,
        "host": "10.0.0.7",
        "port": 8080,
        "username": "u",
        "password_enc": b"x",
        "is_enabled": True,
        "health": ProxyHealth.COOLDOWN,
        "success_count": 12,
        "failure_count": 3,
        "consecutive_failures": 2,
        "last_latency_ms": 340,
        "cooldown_until": datetime(2026, 9, 7, 12, 0, tzinfo=UTC),
        "last_error": "timeout: <redacted>",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_proxies_page_lists_the_pool_with_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ops, "list_proxies", lambda session: [fake_proxy()])
    html = make_client(monkeypatch).get("/admin/proxies", auth=AUTH).text
    assert "http://10.0.0.7:8080" in html
    assert "u · secret" in html
    assert 'class="health-cooldown"' in html
    assert "timeout: &lt;redacted&gt;" in html  # escaped, never raw
    for action in (ops.ProxyAction.DISABLE, ops.ProxyAction.RESET, ops.ProxyAction.REMOVE):
        assert f'action="/admin/proxies/7/{action}"' in html
    assert f'action="/admin/proxies/7/{ops.ProxyAction.ENABLE}"' not in html


def test_proxies_add_and_actions_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []

    def fake_add(session: Any, url: str, label: str | None) -> SimpleNamespace:
        calls.append(("add", url, label))
        return fake_proxy(label=label or "auto")

    def fake_action(session: Any, proxy_id: int, action: ops.ProxyAction) -> str:
        assert isinstance(action, ops.ProxyAction)
        calls.append((action, proxy_id))
        return "p7"

    monkeypatch.setattr(ops, "add_proxy", fake_add)
    monkeypatch.setattr(ops, "proxy_action", fake_action)
    client = make_client(monkeypatch)
    added = client.post(
        "/admin/proxies",
        data={"url": " http://u:s@10.0.0.7:8080 ", "label": ""},
        auth=AUTH,
        follow_redirects=False,
    )
    assert added.headers["location"] == "/admin/proxies?ok=added+auto"
    acted = client.post("/admin/proxies/7/reset", auth=AUTH, follow_redirects=False)
    assert acted.headers["location"] == "/admin/proxies?ok=reset%3A+p7"
    assert client.post("/admin/proxies/7/explode", auth=AUTH).status_code == 404
    assert calls == [("add", "http://u:s@10.0.0.7:8080", None), (ops.ProxyAction.RESET, 7)]


def test_proxies_refusal_is_shown_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(session: Any, url: str, label: str | None) -> SimpleNamespace:
        raise ops.AdminError("already present as p7")

    monkeypatch.setattr(ops, "add_proxy", refuse)
    monkeypatch.setattr(ops, "list_proxies", lambda session: [])
    response = make_client(monkeypatch).post(
        "/admin/proxies", data={"url": "http://h:1"}, auth=AUTH
    )
    assert "already present as p7" in response.text
    assert "No proxies yet." in response.text


def test_screens_page_and_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    from yfin.models.discovery import ScreenKind, ScreenQuoteType

    screen = SimpleNamespace(
        screen_key="day_gainers",
        title="Day gainers",
        kind=ScreenKind.PREDEFINED,
        quote_type=ScreenQuoteType.EQUITY,
        description="Biggest movers",
        is_enabled=False,
    )
    toggled: list[tuple[str, bool]] = []
    monkeypatch.setattr(ops, "list_screens", lambda session: [screen])
    monkeypatch.setattr(
        ops, "set_screen_enabled", lambda session, key, enabled: toggled.append((key, enabled))
    )
    client = make_client(monkeypatch)
    html = client.get("/admin/screens", auth=AUTH).text
    assert "<code>day_gainers</code>" in html
    assert 'action="/admin/screens/day_gainers/enable"' in html
    response = client.post("/admin/screens/day_gainers/enable", auth=AUTH, follow_redirects=False)
    assert response.status_code == 303
    assert toggled == [("day_gainers", True)]
    assert client.post("/admin/screens/day_gainers/vanish", auth=AUTH).status_code == 404


def test_clients_page_is_read_only_and_hides_contact_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_row = SimpleNamespace(
        client_id="acme",
        name="Acme Research",
        plan="pro",
        is_active=True,
        auth_epoch=3,
        last_used_at=None,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        owner_email="hidden",
    )
    monkeypatch.setattr(ops, "list_clients", lambda session: [client_row])
    client = make_client(monkeypatch)
    html = client.get("/admin/clients", auth=AUTH).text
    assert "<code>acme</code>" in html
    assert "hidden" not in html
    assert client.post("/admin/clients/acme/disable", auth=AUTH).status_code in (404, 405)


def test_stylesheet_is_served_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    response = make_client(monkeypatch).get("/admin/admin.css")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
