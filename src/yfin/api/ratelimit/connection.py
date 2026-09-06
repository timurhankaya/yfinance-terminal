"""Redis connection for the API.

One place builds the client so timeouts are not re-invented per call
site. The timeouts are short on purpose: Redis sits in the request path,
and a hung socket there would hold a worker thread far longer than the
counter it is protecting is worth.
"""

from __future__ import annotations

import redis

from yfin.api.core.config import ApiSettings

CONNECT_TIMEOUT_SECONDS = 1.0
OPERATION_TIMEOUT_SECONDS = 1.0


def get_redis(settings: ApiSettings) -> redis.Redis:
    return redis.Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=CONNECT_TIMEOUT_SECONDS,
        socket_timeout=OPERATION_TIMEOUT_SECONDS,
        decode_responses=True,
    )
