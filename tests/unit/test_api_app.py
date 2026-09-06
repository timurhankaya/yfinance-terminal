"""API scaffolding: error shape, security headers, client IP, health."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import Headers
from starlette.requests import Request

from yfin.api.core.config import ApiSettings
from yfin.api.core.errors import install_error_handlers
from yfin.api.core.middleware import SECURITY_HEADERS, _networks, resolve_client_ip
from yfin.api.routers import meta
from yfin.core.logging_setup import REDACTED, redact_secrets


@pytest.fixture
def client() -> TestClient:
    from yfin.api.app import create_app

    return TestClient(create_app(ApiSettings(trusted_proxies="", docs_enabled=True)))


def test_health_ayakta_ve_bagimlilik_yok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_guvenlik_basliklari_her_yanitta(client: TestClient) -> None:
    response = client.get("/health")
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


def test_request_id_basligi_ve_govdede_ayni(client: TestClient) -> None:
    response = client.get("/yok-boyle-bir-yol")
    assert response.headers["X-Request-Id"] == response.json()["request_id"]


def test_404_problem_json_ve_IC_DETAY_YOK(client: TestClient) -> None:
    response = client.get("/yok-boyle-bir-yol")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert set(response.json()) == {"type", "title", "status", "request_id"}


def test_422_GONDERILEN_DEGERI_YANSITMAZ() -> None:
    """FastAPI's default 422 echoes `input`, i.e. whatever the client sent,
    plus the internal field path. A public API must not amplify one and
    must not publish the other."""
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/echo")
    def echo(limit: int) -> dict[str, int]:  # pragma: no cover - never reached
        return {"limit": limit}

    response = TestClient(app).get("/echo", params={"limit": "gizli-deger"})
    assert response.status_code == 422
    body = response.text
    assert "gizli-deger" not in body
    assert "limit" in response.json()["detail"]


def test_islenmemis_istisna_OPAK_500_olur() -> None:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/patla")
    def patla() -> None:
        raise RuntimeError("connection to 10.0.0.5 failed: table api_clients")

    response = TestClient(app, raise_server_exceptions=False).get("/patla")
    assert response.status_code == 500
    assert "api_clients" not in response.text
    assert "10.0.0.5" not in response.text
    assert response.json()["type"] == "internal_error"


# --- istemci IP cozumlemesi -------------------------------------------------


def _request(peer: str, forwarded: str | None = None) -> Request:
    headers = {"x-forwarded-for": forwarded} if forwarded else {}
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": Headers(headers).raw,
            "client": (peer, 1234),
        }
    )


def test_proxy_yapilandirilmamissa_forwarded_baslik_YOK_SAYILIR() -> None:
    """An unconfigured deployment must not be a bypass: without a trusted
    network the header is attacker-controlled input."""
    nets = _networks(ApiSettings(trusted_proxies=""))
    ip = resolve_client_ip(_request("203.0.113.9", forwarded="1.2.3.4"), nets)
    assert ip == "203.0.113.9"


def test_guvenilmeyen_kaynaktan_gelen_forwarded_baslik_YOK_SAYILIR() -> None:
    nets = _networks(ApiSettings(trusted_proxies="10.0.0.0/8"))
    ip = resolve_client_ip(_request("203.0.113.9", forwarded="1.2.3.4"), nets)
    assert ip == "203.0.113.9"


def test_guvenilen_proxy_arkasinda_SAGDAN_atlanarak_secilir() -> None:
    """The client is the rightmost address that is not one of ours;
    anything further left was written by someone we do not control."""
    nets = _networks(ApiSettings(trusted_proxies="10.0.0.0/8"))
    request = _request("10.0.0.1", forwarded="9.9.9.9, 198.51.100.7, 10.0.0.2")
    assert resolve_client_ip(request, nets) == "198.51.100.7"


def test_zincirin_tamami_guvenilirse_peer_adresi_kullanilir() -> None:
    nets = _networks(ApiSettings(trusted_proxies="10.0.0.0/8"))
    request = _request("10.0.0.1", forwarded="10.0.0.3, 10.0.0.2")
    assert resolve_client_ip(request, nets) == "10.0.0.1"


# --- log redaksiyonu --------------------------------------------------------


def test_sir_alanlari_kosulsuz_redakte_edilir() -> None:
    event = redact_secrets(
        None, "info", {"authorization": "Basic abc", "client_secret": "s3cret", "route": "/v1"}
    )
    assert event["authorization"] == REDACTED
    assert event["client_secret"] == REDACTED
    assert event["route"] == "/v1"


# --- imza anahtari ----------------------------------------------------------


def test_kisa_imza_anahtari_REDDEDILIR() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        ApiSettings(jwt_signing_key="kisa").signing_key_bytes()


def test_yeterli_uzunluktaki_anahtar_kabul_edilir() -> None:
    assert ApiSettings(jwt_signing_key="x" * 32).signing_key_bytes() == b"x" * 32


# --- readiness --------------------------------------------------------------


def test_readiness_IP_BASINA_sinirlidir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unauthenticated and touching both the database and Redis, this is
    the cheapest surface in the API; uncapped it would drain the pool."""
    from yfin.api.app import create_app

    monkeypatch.setattr(meta, "_check_database", lambda: True)
    monkeypatch.setattr(meta, "_check_redis", lambda _s: True)
    monkeypatch.setattr(meta, "_limiter", meta._FixedWindow())
    monkeypatch.setattr(meta, "_cache", meta._ReadinessCache())

    client = TestClient(create_app(ApiSettings(health_rate_limit_per_minute=2)))
    assert client.get("/health/ready").status_code == 200
    assert client.get("/health/ready").status_code == 200
    limited = client.get("/health/ready")
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "60"
    assert limited.json()["type"] == "rate_limit_exceeded"


def test_readiness_sonucu_onbelleklenir(monkeypatch: pytest.MonkeyPatch) -> None:
    from yfin.api.app import create_app

    calls = {"db": 0}

    def counted_db() -> bool:
        calls["db"] += 1
        return True

    monkeypatch.setattr(meta, "_check_database", counted_db)
    monkeypatch.setattr(meta, "_check_redis", lambda _s: True)
    monkeypatch.setattr(meta, "_limiter", meta._FixedWindow())
    monkeypatch.setattr(meta, "_cache", meta._ReadinessCache())

    client = TestClient(create_app(ApiSettings(health_cache_seconds=30)))
    client.get("/health/ready")
    client.get("/health/ready")
    assert calls["db"] == 1


def test_readiness_bagimlilik_dusunce_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    from yfin.api.app import create_app

    monkeypatch.setattr(meta, "_check_database", lambda: True)
    monkeypatch.setattr(meta, "_check_redis", lambda _s: False)
    monkeypatch.setattr(meta, "_limiter", meta._FixedWindow())
    monkeypatch.setattr(meta, "_cache", meta._ReadinessCache())

    response = TestClient(create_app(ApiSettings())).get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "degraded", "database": "ok", "redis": "fail"}
