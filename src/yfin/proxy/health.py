"""Proxy health policy: a pure state machine.

No DB or network dependency, so it can be tested with a full transition
table. Parent and child both call the same `apply_outcome`."""

from __future__ import annotations

import enum
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from yfin.core.config import Settings, get_settings
from yfin.core.errors import PROXY_FAULT_KINDS, ErrorKind
from yfin.models import Proxy, ProxyHealth


class HealthEvent(enum.StrEnum):
    SUCCESS = "success"
    RATE_LIMITED = "rate_limited"
    BLOCKED = "blocked"
    NETWORK = "network"
    # Child died unexpectedly or timed out. Triggers cooldown regardless
    # of the failure threshold: a single NETWORK event would only
    # increment the counter, leaving the crashed shard's proxy in the pool.
    SHARD_CRASH = "shard_crash"


_EVENT_BY_KIND = {
    ErrorKind.RATE_LIMITED: HealthEvent.RATE_LIMITED,
    ErrorKind.BLOCKED: HealthEvent.BLOCKED,
    ErrorKind.NETWORK: HealthEvent.NETWORK,
}


def event_for(kind: ErrorKind) -> HealthEvent | None:
    """DATA and UNKNOWN_SYMBOL never affect proxy health."""
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
    """Applies a single event to the state machine.

    SUCCESS does not reset `cooldown_rounds`: one lucky success would keep
    restarting the path to dead, leaving a half-dead proxy in the pool forever."""
    if state.health is ProxyHealth.DEAD:
        return state  # only `proxy reset` brings it back

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
# Selection
# --------------------------------------------------------------------------
