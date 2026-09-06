"""Proxy havuzu: DSN, redaction, sifreleme ve saglik durum makinesi (P10.1).

Durum makinesi SAF bir fonksiyondur; bu dosya DB'ye hic dokunmaz.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from yfin.core.config import Settings
from yfin.core.errors import ErrorKind
from yfin.models import ProxyHealth, ProxyScheme
from yfin.proxy import (
    HealthEvent,
    PasswordUndecryptable,
    ProxyEndpoint,
    ProxyHealthState,
    ProxyPolicy,
    SecretKeyMissing,
    apply_outcome,
    decrypt_password,
    default_label,
    encrypt_password,
    event_for,
    parse_dsn,
)

NOW = datetime(2026, 9, 4, 12, 0, 0)
POLICY = ProxyPolicy(failure_threshold=3, cooldown_seconds=900, dead_rounds=3)

FAULTS = [HealthEvent.RATE_LIMITED, HealthEvent.BLOCKED, HealthEvent.NETWORK]

SECRET = "hunter2"


def _settings(key: str = "") -> Settings:
    return Settings(yf_proxy_secret_key=key, _env_file=None)  # type: ignore[call-arg]


# --- DSN -------------------------------------------------------------------


class TestDsn:
    def test_parses_all_schemes(self) -> None:
        for scheme in ProxyScheme:
            endpoint = parse_dsn(f"{scheme.value}://10.0.0.1:8080")
            assert endpoint.scheme is scheme
            assert endpoint.port == 8080

    def test_percent_encoded_credentials_round_trip(self) -> None:
        """Kimlik bilgisindeki ':' ve '/' kodlanmis gelir; dsn() yeniden
        kodlar, aksi halde ayirici sanilirdi."""
        endpoint = parse_dsn("socks5h://ac%3Act:pa%2Fss@10.0.0.1:1080")
        assert endpoint.username == "ac:ct"
        assert endpoint.password == "pa/ss"
        assert parse_dsn(endpoint.dsn()) == endpoint

    def test_rejects_unknown_scheme_and_missing_port(self) -> None:
        with pytest.raises(ValueError, match="unsupported scheme"):
            parse_dsn("ftp://10.0.0.1:21")
        with pytest.raises(ValueError, match="port"):
            parse_dsn("http://10.0.0.1")

    def test_password_never_leaks_through_repr(self) -> None:
        """P3.4: parola repr'e, str'ye ve log satirina GIRMEZ."""
        endpoint = parse_dsn(f"http://acct:{SECRET}@10.0.0.1:3128")
        assert SECRET not in repr(endpoint)
        assert SECRET not in str(endpoint)
        assert SECRET not in endpoint.redacted()
        assert "acct:***@" in endpoint.redacted()
        # ...ama gercek DSN parolayi TASIR, aksi halde proxy calismazdi
        assert SECRET in endpoint.dsn()

    def test_default_label_has_no_dots(self) -> None:
        assert default_label(parse_dsn("http://10.0.0.1:8080")) == "10-0-0-1-8080"


# --- sifreleme -------------------------------------------------------------


class TestPasswordEncryption:
    def test_round_trip(self) -> None:
        from cryptography.fernet import Fernet

        settings = _settings(Fernet.generate_key().decode())
        token = encrypt_password(SECRET, settings)
        assert token is not None
        assert SECRET.encode() not in token
        assert decrypt_password(token, settings) == SECRET

    def test_empty_password_stores_nothing(self) -> None:
        assert encrypt_password("", _settings()) is None
        assert decrypt_password(None, _settings()) == ""

    def test_missing_key_is_explicit_error(self) -> None:
        with pytest.raises(SecretKeyMissing):
            encrypt_password("x", _settings())

    def test_rotated_key_is_reported_not_silently_empty(self) -> None:
        """Anahtar degisirse proxy dead YAPILMAZ; acik hata verilir (P3.4)."""
        from cryptography.fernet import Fernet

        token = encrypt_password("x", _settings(Fernet.generate_key().decode()))
        assert token is not None
        with pytest.raises(PasswordUndecryptable):
            decrypt_password(token, _settings(Fernet.generate_key().decode()))


# --- saglik durum makinesi -------------------------------------------------


class TestHealthStateMachine:
    def test_success_marks_healthy_and_clears_streak(self) -> None:
        state = ProxyHealthState(consecutive_failures=2)
        after = apply_outcome(state, HealthEvent.SUCCESS, NOW, POLICY)
        assert after.health is ProxyHealth.HEALTHY
        assert after.consecutive_failures == 0
        assert after.cooldown_until is None

    @pytest.mark.parametrize("event", FAULTS)
    def test_single_fault_only_increments(self, event: HealthEvent) -> None:
        after = apply_outcome(ProxyHealthState(), event, NOW, POLICY)
        assert after.health is ProxyHealth.UNKNOWN
        assert after.consecutive_failures == 1

    @pytest.mark.parametrize("event", FAULTS)
    def test_threshold_triggers_cooldown(self, event: HealthEvent) -> None:
        state = ProxyHealthState()
        for _ in range(POLICY.failure_threshold):
            state = apply_outcome(state, event, NOW, POLICY)
        assert state.health is ProxyHealth.COOLDOWN
        assert state.cooldown_rounds == 1
        assert state.consecutive_failures == 0
        assert state.cooldown_until == NOW + timedelta(seconds=POLICY.cooldown_seconds)

    def test_shard_crash_bypasses_threshold(self) -> None:
        """Tek NETWORK olayi cooldown uretmezdi; olen shard'in proxy'si
        havuzda kalmaya devam ederdi (P5.4)."""
        after = apply_outcome(ProxyHealthState(), HealthEvent.SHARD_CRASH, NOW, POLICY)
        assert after.health is ProxyHealth.COOLDOWN
        assert after.cooldown_rounds == 1

    def test_dead_after_configured_rounds(self) -> None:
        state = ProxyHealthState()
        for _ in range(POLICY.dead_rounds):
            state = apply_outcome(state, HealthEvent.SHARD_CRASH, NOW, POLICY)
        assert state.health is ProxyHealth.DEAD

    def test_dead_is_terminal_until_reset(self) -> None:
        state = ProxyHealthState(health=ProxyHealth.DEAD)
        assert apply_outcome(state, HealthEvent.SUCCESS, NOW, POLICY) == state

    def test_success_does_not_reset_cooldown_rounds(self) -> None:
        """Sifirlasaydi arada tek bir basari dead'e giden yolu surekli
        bastan baslatir, yari-olu proxy sonsuza dek havuzda kalirdi."""
        state = ProxyHealthState(health=ProxyHealth.COOLDOWN, cooldown_rounds=2)
        after = apply_outcome(state, HealthEvent.SUCCESS, NOW, POLICY)
        assert after.cooldown_rounds == 2

    def test_expired_cooldown_keeps_cooldown_health(self) -> None:
        """health 'cooldown' KALIR; yalnizca ilk basari healthy yapar.

        Uygunlugun kendisi SQL'de tanimlidir (tek dogruluk kaynagi) ve
        repo testindeki dogruluk tablosuyla sinanir.
        """
        state = ProxyHealthState(
            health=ProxyHealth.COOLDOWN, cooldown_until=NOW - timedelta(seconds=1)
        )
        after = apply_outcome(state, HealthEvent.SUCCESS, NOW, POLICY)
        assert state.health is ProxyHealth.COOLDOWN
        assert after.health is ProxyHealth.HEALTHY


class TestEventMapping:
    @pytest.mark.parametrize(
        ("kind", "expected"),
        [
            (ErrorKind.RATE_LIMITED, HealthEvent.RATE_LIMITED),
            (ErrorKind.BLOCKED, HealthEvent.BLOCKED),
            (ErrorKind.NETWORK, HealthEvent.NETWORK),
        ],
    )
    def test_transport_faults_map_to_events(self, kind: ErrorKind, expected: HealthEvent) -> None:
        assert event_for(kind) is expected

    @pytest.mark.parametrize("kind", [ErrorKind.DATA, ErrorKind.UNKNOWN_SYMBOL])
    def test_data_faults_never_punish_proxy(self, kind: ErrorKind) -> None:
        """Gecersiz sembol veya parse hatasi proxy'nin sucu DEGILDIR."""
        assert event_for(kind) is None


class TestEndpointEquality:
    def test_endpoint_is_value_object(self) -> None:
        left = ProxyEndpoint(ProxyScheme.HTTP, "10.0.0.1", 1, "u", "p")
        right = ProxyEndpoint(ProxyScheme.HTTP, "10.0.0.1", 1, "u", "p")
        assert left == right
