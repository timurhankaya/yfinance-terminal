"""Proxy havuzu.

Sorumluluklar ayri modullerde: `dsn` (deger nesnesi), `crypto`
(sifreleme), `health` (SAF durum makinesi), `repository` (DB), `check`
(ag). Disaridan tek bir yuz gorunur.
"""

from __future__ import annotations

from yfin.proxy.check import CHECK_CHART_URL, CHECK_CRUMB_URL, CheckResult, check_endpoint
from yfin.proxy.crypto import (
    PasswordUndecryptable,
    SecretKeyMissing,
    decrypt_password,
    encrypt_password,
    endpoint_of,
)
from yfin.proxy.dsn import ProxyEndpoint, default_label, parse_dsn
from yfin.proxy.health import (
    HealthEvent,
    ProxyHealthState,
    ProxyPolicy,
    apply_outcome,
    event_for,
)
from yfin.proxy.repository import (
    ShardProxyTracker,
    count_all,
    persist_event,
    select_eligible,
)

__all__ = [
    "CHECK_CHART_URL",
    "CHECK_CRUMB_URL",
    "CheckResult",
    "HealthEvent",
    "PasswordUndecryptable",
    "ProxyEndpoint",
    "ProxyHealthState",
    "ProxyPolicy",
    "SecretKeyMissing",
    "ShardProxyTracker",
    "apply_outcome",
    "check_endpoint",
    "count_all",
    "decrypt_password",
    "default_label",
    "encrypt_password",
    "endpoint_of",
    "event_for",
    "parse_dsn",
    "persist_event",
    "select_eligible",
]
