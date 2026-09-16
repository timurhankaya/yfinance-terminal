"""Redis connection for the API. Timeouts are short on purpose: Redis sits
in the request path and a hung socket would hold a worker thread."""

from __future__ import annotations

import functools

import redis

from yfin.api.core.config import ApiSettings

CONNECT_TIMEOUT_SECONDS = 1.0
OPERATION_TIMEOUT_SECONDS = 1.0


@functools.lru_cache(maxsize=4)
def _client(url: str) -> redis.Redis:
    """One client, and therefore one connection pool, per URL: `from_url`
    builds a new pool every call and a metered request calls this several
    times. Keyed on the URL so it stays hashable."""
    return redis.Redis.from_url(
        url,
        socket_connect_timeout=CONNECT_TIMEOUT_SECONDS,
        socket_timeout=OPERATION_TIMEOUT_SECONDS,
        decode_responses=True,
    )


def get_redis(settings: ApiSettings) -> redis.Redis:
    return _client(settings.redis_url)
