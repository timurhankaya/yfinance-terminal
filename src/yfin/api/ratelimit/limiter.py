"""Per-second token bucket and monthly quota, decided in one Lua script so
concurrent requests cannot slip past either. The quota is checked first
and refused without consuming a token; INCR and EXPIRE are in the same
script, or a crash between them leaves a counter that never resets.
Everything here fails open (the token endpoint does the opposite)."""

from __future__ import annotations

import calendar
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from redis import Redis

from yfin.api.core.config import ApiSettings
from yfin.api.ratelimit.connection import get_redis
from yfin.api.ratelimit.policy import PlanLimits
from yfin.core.logging_setup import get_logger
from yfin.core.metrics import inc

log = get_logger(__name__)

BUCKET_KEY = "rl:{client_id}"
QUOTA_KEY = "quota:{client_id}:{period}"

REASON_ALLOWED = 0
REASON_RATE = 1
REASON_QUOTA = 2

#: The Lua script speaks in integers because that is what fits in a Redis
#: reply; a metric labelled `reason="1"` would be a number nobody can read
#: on a dashboard, so the mapping happens once, here.
REASON_NAMES: dict[int, str] = {
    REASON_ALLOWED: "allowed",
    REASON_RATE: "rate",
    REASON_QUOTA: "quota",
}

# KEYS: bucket, quota
# ARGV: now_ms, rate, burst, quota_limit, quota_ttl_seconds
# Returns: {allowed, retry_after_ms, tokens_left, quota_used, reason}
_SCRIPT = """
local bucket_key = KEYS[1]
local quota_key = KEYS[2]
local now = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local burst = tonumber(ARGV[3])
local quota_limit = tonumber(ARGV[4])
local quota_ttl = tonumber(ARGV[5])

local used = tonumber(redis.call('GET', quota_key) or '0')
if used >= quota_limit then
  return {0, quota_ttl * 1000, 0, used, 2}
end

local state = redis.call('HMGET', bucket_key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil or ts == nil then
  tokens = burst
  ts = now
end

local elapsed = math.max(0, now - ts) / 1000.0
tokens = math.min(burst, tokens + elapsed * rate)
local idle_ttl = math.ceil(burst / rate * 1000) + 1000

if tokens < 1 then
  redis.call('HSET', bucket_key, 'tokens', tokens, 'ts', now)
  redis.call('PEXPIRE', bucket_key, idle_ttl)
  return {0, math.ceil((1 - tokens) / rate * 1000), 0, used, 1}
end

tokens = tokens - 1
redis.call('HSET', bucket_key, 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', bucket_key, idle_ttl)

used = redis.call('INCR', quota_key)
if used == 1 then
  redis.call('EXPIRE', quota_key, quota_ttl)
end

return {1, 0, math.floor(tokens), used, 0}
"""


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: int
    retry_after: int
    tokens_left: int
    quota_used: int
    quota_limit: int
    quota_reset: int
    rate_limit: int
    #: True when the counter store was unreachable and the request was
    #: let through unmetered. The usage row is marked estimated.
    degraded: bool = False


def period_key(now: datetime | None = None) -> str:
    """`YYYY-MM` in UTC. The window is UTC everywhere, deliberately: a
    per-client local month would make two clients' usage incomparable and
    put the reset at a different instant for each."""
    return (now or datetime.now(UTC)).strftime("%Y-%m")


def seconds_until_month_end(now: datetime | None = None) -> int:
    moment = now or datetime.now(UTC)
    days_in_month = calendar.monthrange(moment.year, moment.month)[1]
    end = moment.replace(
        day=days_in_month, hour=23, minute=59, second=59, microsecond=0
    )
    return max(1, int((end - moment).total_seconds()) + 1)


def consume(settings: ApiSettings, client_id: str, limits: PlanLimits) -> Verdict:
    quota_ttl = seconds_until_month_end()
    quota_key = QUOTA_KEY.format(client_id=client_id, period=period_key())
    try:
        redis: Redis = get_redis(settings)
        allowed, retry_ms, tokens_left, quota_used, reason = redis.eval(
            _SCRIPT,
            2,
            BUCKET_KEY.format(client_id=client_id),
            quota_key,
            int(time.time() * 1000),
            limits.requests_per_second,
            limits.burst,
            limits.monthly_quota,
            quota_ttl,
        )
    except Exception as exc:  # noqa: BLE001 - fail open, see the module docstring
        log.error("rate_limit_failed_open", client_id=client_id, error=str(exc))
        # Two counters, because they answer two questions. The request WAS
        # allowed, so it belongs in the decisions; and it was allowed
        # because the store was gone, which is what `where=limiter` says.
        inc("yfin_api_redis_failopen_total", where="limiter")
        inc("yfin_api_ratelimit_decisions_total", reason=REASON_NAMES[REASON_ALLOWED])
        return Verdict(
            allowed=True,
            reason=REASON_ALLOWED,
            retry_after=0,
            tokens_left=limits.burst,
            quota_used=0,
            quota_limit=limits.monthly_quota,
            quota_reset=quota_ttl,
            rate_limit=limits.requests_per_second,
            degraded=True,
        )

    inc(
        "yfin_api_ratelimit_decisions_total",
        reason=REASON_NAMES.get(int(reason), "allowed"),
    )
    return Verdict(
        allowed=bool(allowed),
        reason=int(reason),
        # Whole seconds, rounded up: Retry-After has no sub-second form,
        # and rounding down would invite a client straight back into a
        # refusal.
        retry_after=max(1, -(-int(retry_ms) // 1000)) if not allowed else 0,
        tokens_left=int(tokens_left),
        quota_used=int(quota_used),
        quota_limit=limits.monthly_quota,
        quota_reset=quota_ttl,
        rate_limit=limits.requests_per_second,
    )


def refund_quota(settings: ApiSettings, client_id: str) -> None:
    """Gives back one quota unit after a server-side failure. The token
    bucket is not refunded: the request really did cost infrastructure."""
    try:
        redis = get_redis(settings)
        key = QUOTA_KEY.format(client_id=client_id, period=period_key())
        # Never below zero: a refund arriving after a month boundary would
        # otherwise open the next month with a negative counter.
        if int(redis.get(key) or 0) > 0:
            redis.decr(key)
    except Exception as exc:  # noqa: BLE001 - best effort, like the counters
        log.error("quota_refund_failed", client_id=client_id, error=str(exc))


def headers(verdict: Verdict) -> dict[str, str]:
    """Rate headers plus a separate quota family; they reset on different
    clocks, and folding the monthly quota into `RateLimit-*` would tell a
    client its per-second budget was millions."""
    return {
        "RateLimit-Limit": str(verdict.rate_limit),
        "RateLimit-Remaining": str(verdict.tokens_left),
        "RateLimit-Reset": str(verdict.retry_after if not verdict.allowed else 1),
        "X-Quota-Limit": str(verdict.quota_limit),
        "X-Quota-Remaining": str(max(0, verdict.quota_limit - verdict.quota_used)),
        "X-Quota-Reset": str(verdict.quota_reset),
    }
