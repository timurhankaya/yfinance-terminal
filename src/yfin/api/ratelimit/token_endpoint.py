"""The token endpoint's own limiter, the one fail-closed spot in the API:
it is the only brake on an argon2 verification anyone can trigger, so
without Redis an in-process limiter takes over and 503 follows. The
primary key is the address; `client_id` is attacker-supplied, so it only
ever slows down, and every key is a fixed-length digest with a TTL."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from yfin.api.core.config import ApiSettings
from yfin.api.ratelimit.connection import get_redis
from yfin.api.ratelimit.fixed_window import FixedWindow
from yfin.core.logging_setup import get_logger

log = get_logger(__name__)

WINDOW_SECONDS = 60

#: Per address, per window. The primary brake.
IP_LIMIT = 30
#: Consecutive failures for one client id before it is slowed down.
FAILURE_LIMIT = 10
#: How long that slow-down lasts.
FAILURE_BLOCK_SECONDS = 900
#: The in-process fallback when Redis is unreachable. Deliberately
#: tighter: it is per process, so several workers multiply it.
FALLBACK_IP_LIMIT = 10

IP_KEY = "tok:ip:{digest}"
FAIL_KEY = "tok:fail:{digest}"
OK_KEY = "tok:ok:{digest}"


def _digest(value: str) -> str:
    """Fixed-length key material from unverified input."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after: int = WINDOW_SECONDS
    #: True when Redis is gone and even the fallback is exhausted; the
    #: endpoint answers 503 rather than 429, because the refusal is ours,
    #: not the caller's fault.
    degraded: bool = False


_fallback = FixedWindow(WINDOW_SECONDS)


def check(settings: ApiSettings, *, client_ip: str, client_id: str) -> Decision:
    """Called before any hashing work is done."""
    ip_digest = _digest(client_ip)
    id_digest = _digest(client_id)
    try:
        redis = get_redis(settings)
        pipe = redis.pipeline()
        pipe.incr(IP_KEY.format(digest=ip_digest))
        pipe.expire(IP_KEY.format(digest=ip_digest), WINDOW_SECONDS)
        pipe.get(FAIL_KEY.format(digest=id_digest))
        ip_hits, _, failures = pipe.execute()
    except Exception as exc:  # noqa: BLE001 - fail-closed, see module docstring
        log.error("token_limiter_degraded", error=str(exc))
        if _fallback.allow(ip_digest, FALLBACK_IP_LIMIT):
            return Decision(allowed=True)
        return Decision(allowed=False, degraded=True)

    if int(ip_hits) > IP_LIMIT:
        return Decision(allowed=False)
    if failures is not None and int(failures) >= FAILURE_LIMIT:
        return Decision(allowed=False, retry_after=FAILURE_BLOCK_SECONDS)
    return Decision(allowed=True)


def record_failure(settings: ApiSettings, client_id: str) -> None:
    """Counts a failed authentication for this client id. Slows the id down,
    never blocks it: the party slowed may be the victim of a spray."""
    key = FAIL_KEY.format(digest=_digest(client_id))
    try:
        redis = get_redis(settings)
        pipe = redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, FAILURE_BLOCK_SECONDS)
        pipe.execute()
    except Exception as exc:  # noqa: BLE001 - counters are best effort
        log.error("token_failure_not_counted", error=str(exc))


def record_success(settings: ApiSettings, client_id: str) -> None:
    """Clears the failure counter and counts the issue."""
    id_digest = _digest(client_id)
    try:
        redis = get_redis(settings)
        pipe = redis.pipeline()
        pipe.delete(FAIL_KEY.format(digest=id_digest))
        pipe.incr(OK_KEY.format(digest=id_digest))
        pipe.expire(OK_KEY.format(digest=id_digest), WINDOW_SECONDS)
        pipe.execute()
    except Exception as exc:  # noqa: BLE001 - counters are best effort
        log.error("token_success_not_counted", error=str(exc))
