"""Plan limits from `api_plans`, cached in-process behind a short TTL so the
request path pays no query and an edit needs no restart. The token does
not carry the plan: a downgrade must not wait for issued tokens to
expire."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from sqlalchemy import select

from yfin.api.models.clients import ApiClient
from yfin.api.models.plans import ApiPlan
from yfin.api.storage.session import get_session_factory
from yfin.core.logging_setup import get_logger
from yfin.core.metrics import inc

log = get_logger(__name__)

CACHE_TTL_SECONDS = 60.0


@dataclass(frozen=True)
class PlanLimits:
    plan: str
    requests_per_second: int
    burst: int
    monthly_quota: int
    max_page_size: int
    max_concurrency: int


#: Used when a client's plan row has vanished (a plan deleted out from
#: under a live client). Deliberately restrictive rather than permissive:
#: a misconfiguration should throttle, not open the gates.
FALLBACK = PlanLimits(
    plan="unknown",
    requests_per_second=1,
    burst=2,
    monthly_quota=1_000,
    max_page_size=100,
    max_concurrency=1,
)


class _TtlCache:
    def __init__(self, ttl: float) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, PlanLimits]] = {}

    def get(self, key: str) -> PlanLimits | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry[0] < time.monotonic():
                return None
            return entry[1]

    def put(self, key: str, value: PlanLimits) -> None:
        with self._lock:
            self._entries[key] = (time.monotonic() + self._ttl, value)


_cache = _TtlCache(CACHE_TTL_SECONDS)


def limits_for_client(client_id: str) -> PlanLimits:
    cached = _cache.get(client_id)
    # A miss is a two-table join on the request path, so the ratio is the
    # number that says whether the TTL is set anywhere near right.
    inc(
        "yfin_cache_ops_total",
        cache="plan_limits",
        result="miss" if cached is None else "hit",
    )
    if cached is not None:
        return cached

    with get_session_factory()() as session:
        row = session.execute(
            select(
                ApiPlan.plan,
                ApiPlan.requests_per_second,
                ApiPlan.burst,
                ApiPlan.monthly_quota,
                ApiPlan.max_page_size,
                ApiPlan.max_concurrency,
            )
            .join(ApiClient, ApiClient.plan == ApiPlan.plan)
            .where(ApiClient.client_id == client_id)
        ).first()

    if row is None:
        log.error("plan_row_missing", client_id=client_id)
        limits = FALLBACK
    else:
        limits = PlanLimits(*row)

    _cache.put(client_id, limits)
    return limits
