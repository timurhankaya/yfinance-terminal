"""Per-client concurrency slots.

Rate limits bound how *often* a client calls, not how many calls it keeps
open at once. Those are different resources: the synchronous design (K1)
serves requests from a fixed thread pool, so a handful of slow queries
held open simultaneously can occupy every worker while the caller stays
comfortably inside its per-second rate. This is the layer that stops
that.

Slots are counted in Redis with a TTL, not held as a lease. A crashed
worker cannot leak a slot forever: the counter expires on its own.
"""

from __future__ import annotations

from dataclasses import dataclass

from yfin.api.core.config import ApiSettings
from yfin.api.ratelimit.connection import get_redis
from yfin.core.logging_setup import get_logger

log = get_logger(__name__)

SLOT_KEY = "conc:{client_id}"

#: Ceiling on how long one request may hold a slot. Above the query
#: timeout, so a slot outlives the request it belongs to but not by much.
SLOT_TTL_SECONDS = 30


@dataclass(frozen=True)
class Slot:
    acquired: bool
    #: False when Redis was unreachable; the request proceeds unmetered.
    degraded: bool = False


def acquire(settings: ApiSettings, client_id: str, limit: int) -> Slot:
    key = SLOT_KEY.format(client_id=client_id)
    try:
        redis = get_redis(settings)
        pipe = redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, SLOT_TTL_SECONDS)
        held, _ = pipe.execute()
    except Exception as exc:  # noqa: BLE001 - fails open, like the counters
        log.error("concurrency_failed_open", client_id=client_id, error=str(exc))
        return Slot(acquired=True, degraded=True)

    if int(held) > limit:
        # Give the slot straight back; the request is being refused, so it
        # must not keep occupying one.
        release(settings, client_id)
        return Slot(acquired=False)
    return Slot(acquired=True)


def release(settings: ApiSettings, client_id: str) -> None:
    key = SLOT_KEY.format(client_id=client_id)
    try:
        redis = get_redis(settings)
        # Never below zero: a release whose acquire expired would
        # otherwise leave the client permanently in credit.
        if int(redis.get(key) or 0) > 0:
            redis.decr(key)
    except Exception as exc:  # noqa: BLE001 - best effort
        log.error("concurrency_release_failed", client_id=client_id, error=str(exc))
