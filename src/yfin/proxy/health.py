"""Proxy saglik POLITIKASI: saf durum makinesi.

DB'siz ve agsiz oldugu icin tam gecis tablosuyla test edilebilir. Parent
da child de ayni `apply_outcome` fonksiyonunu kullanir; politika tek
yerde kalir.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from yfin.config import Settings, get_settings
from yfin.errors import PROXY_FAULT_KINDS, ErrorKind
from yfin.models import Proxy, ProxyHealth


class HealthEvent(enum.StrEnum):
    SUCCESS = "success"
    RATE_LIMITED = "rate_limited"
    BLOCKED = "blocked"
    NETWORK = "network"
    # Child beklenmedik sekilde oldu/timeout'a girdi. ESIKTEN BAGIMSIZ
    # olarak cooldown uretir: tek bir NETWORK olayi yalnizca sayaci bir
    # artirirdi ve olen shard'in proxy'si havuzda kalmaya devam ederdi.
    SHARD_CRASH = "shard_crash"


_EVENT_BY_KIND = {
    ErrorKind.RATE_LIMITED: HealthEvent.RATE_LIMITED,
    ErrorKind.BLOCKED: HealthEvent.BLOCKED,
    ErrorKind.NETWORK: HealthEvent.NETWORK,
}


def event_for(kind: ErrorKind) -> HealthEvent | None:
    """DATA ve UNKNOWN_SYMBOL proxy durumuna HIC dokunmaz."""
    return _EVENT_BY_KIND.get(kind) if kind in PROXY_FAULT_KINDS else None


@dataclass(frozen=True)
class ProxyPolicy:
    failure_threshold: int
    cooldown_seconds: int
    dead_rounds: int

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> ProxyPolicy:
        cfg = settings or get_settings()
        return cls(
            failure_threshold=cfg.yf_proxy_failure_threshold,
            cooldown_seconds=cfg.yf_proxy_cooldown_seconds,
            dead_rounds=cfg.yf_proxy_dead_rounds,
        )


@dataclass(frozen=True)
class ProxyHealthState:
    health: ProxyHealth = ProxyHealth.UNKNOWN
    consecutive_failures: int = 0
    cooldown_rounds: int = 0
    cooldown_until: datetime | None = None

    @classmethod
    def of(cls, row: Proxy) -> ProxyHealthState:
        return cls(
            health=ProxyHealth(row.health),
            consecutive_failures=row.consecutive_failures,
            cooldown_rounds=row.cooldown_rounds,
            cooldown_until=row.cooldown_until,
        )


def _to_cooldown(state: ProxyHealthState, now: datetime, policy: ProxyPolicy) -> ProxyHealthState:
    rounds = state.cooldown_rounds + 1
    health = ProxyHealth.DEAD if rounds >= policy.dead_rounds else ProxyHealth.COOLDOWN
    return replace(
        state,
        health=health,
        consecutive_failures=0,
        cooldown_rounds=rounds,
        cooldown_until=now + timedelta(seconds=policy.cooldown_seconds),
    )


def apply_outcome(
    state: ProxyHealthState,
    event: HealthEvent,
    now: datetime,
    policy: ProxyPolicy,
) -> ProxyHealthState:
    """Tek bir olayi durum makinesine uygular.

    NOT: SUCCESS `cooldown_rounds`'u SIFIRLAMAZ. Sifirlasaydi arada tek
    bir basari dead'e giden yolu surekli bastan baslatir ve yari-olu bir
    proxy sonsuza dek havuzda kalirdi.
    """
    if state.health is ProxyHealth.DEAD:
        return state  # yalnizca `proxy reset` geri getirir

    if event is HealthEvent.SUCCESS:
        return replace(
            state,
            health=ProxyHealth.HEALTHY,
            consecutive_failures=0,
            cooldown_until=None,
        )

    if event is HealthEvent.SHARD_CRASH:
        return _to_cooldown(state, now, policy)

    failures = state.consecutive_failures + 1
    if failures >= policy.failure_threshold:
        return _to_cooldown(replace(state, consecutive_failures=failures), now, policy)
    return replace(state, consecutive_failures=failures)


# --------------------------------------------------------------------------
# Secim
# --------------------------------------------------------------------------
