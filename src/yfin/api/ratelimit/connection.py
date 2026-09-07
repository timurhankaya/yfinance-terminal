"""Redis connection for the API.

One place builds the client so timeouts are not re-invented per call
site. The timeouts are short on purpose: Redis sits in the request path,
and a hung socket there would hold a worker thread far longer than the
counter it is protecting is worth.
"""

from __future__ import annotations

import functools

import redis

from yfin.api.core.config import ApiSettings

CONNECT_TIMEOUT_SECONDS = 1.0
OPERATION_TIMEOUT_SECONDS = 1.0


@functools.lru_cache(maxsize=4)
def _client(url: str) -> redis.Redis:
    """One client, and therefore one connection pool, per URL.

    `from_url` builds a NEW pool every call, and a metered request calls
    this five or six times -- the authenticator, the rate limiter, the
    concurrency slot on the way in and again on the way out, the usage
    counter. Each pool opened its own socket on first use and was closed
    only by the garbage collector, so under load the API worked its way
    through file descriptors for no reason at all, and the very timeouts
    this module exists to keep short started appearing in the request
    path.

    Keyed on the URL rather than the settings object so it stays hashable,
    and capped rather than unbounded: a process serves one Redis, and a
    handful of entries covers a test suite building several apps.
    `storage/session.py` caches the database engine the same way.
    """
    return redis.Redis.from_url(
        url,
        socket_connect_timeout=CONNECT_TIMEOUT_SECONDS,
        socket_timeout=OPERATION_TIMEOUT_SECONDS,
        decode_responses=True,
    )


def get_redis(settings: ApiSettings) -> redis.Redis:
    return _client(settings.redis_url)
