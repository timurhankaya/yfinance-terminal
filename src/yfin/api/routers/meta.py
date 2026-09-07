"""Liveness and readiness.

Both are unauthenticated, and that makes `/health/ready` the cheapest
attack surface in the API: it touches PostgreSQL and Redis on every call.
Left uncapped, a few thousand requests a second would drain the
connection pool and take real traffic down with it. So the result is
cached for a few seconds and the endpoint carries its own per-IP limit.

That limit is in-process on purpose. A readiness probe that needs Redis
in order to report that Redis is down would be useless exactly when it
matters.
"""

from __future__ import annotations

import threading
import time
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import text

from yfin.api.core.config import ApiSettings
from yfin.api.core.errors import TYPE_RATE_LIMIT, ApiProblem
from yfin.api.core.openapi import contract
from yfin.api.core.window import FixedWindow
from yfin.core.logging_setup import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["meta"])

Status = Literal["ok", "degraded"]


class Health(BaseModel):
    status: Status


class Readiness(BaseModel):
    status: Status
    database: Literal["ok", "fail"]
    redis: Literal["ok", "fail"]


class _ReadinessCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value: Readiness | None = None
        self._expires = 0.0

    def get(self) -> Readiness | None:
        with self._lock:
            if self._value is not None and time.monotonic() < self._expires:
                return self._value
            return None

    def put(self, value: Readiness, ttl: float) -> None:
        with self._lock:
            self._value = value
            self._expires = time.monotonic() + ttl


#: Kept under the old name: tests and the readiness endpoint reach it
#: through this module, and moving the class must not move them.
_FixedWindow = FixedWindow

_limiter = _FixedWindow()
_cache = _ReadinessCache()


def _check_database() -> bool:
    from yfin.core.config import bootstrap_settings
    from yfin.storage.db import create_db_engine

    try:
        engine = create_db_engine(bootstrap_settings(), application_name="yfin-api-health")
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - a readiness probe reports, never raises
        log.warning("health_database_unreachable", exc_info=True)
        return False


def _check_redis(settings: ApiSettings) -> bool:
    try:
        import redis

        client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=1)
        client.ping()
        return True
    except Exception:  # noqa: BLE001 - same contract as the database probe
        log.warning("health_redis_unreachable", exc_info=True)
        return False


@router.get(
    "/health",
    response_model=Health,
    summary="Liveness",
    openapi_extra=contract(),
)
def health() -> Health:
    """Answers whether the process is up. Touches nothing else, so it
    stays truthful while dependencies are down."""
    return Health(status="ok")


@router.get(
    "/health/ready",
    response_model=Readiness,
    summary="Readiness",
    openapi_extra=contract(),
)
def health_ready(request: Request) -> Readiness:
    """Answers whether the process can serve traffic: PostgreSQL and Redis
    are both reachable. The result is cached for a few seconds and the
    endpoint carries its own per-IP limit, because it is unauthenticated
    and touches both dependencies on every call."""
    settings: ApiSettings = request.app.state.api_settings
    client_ip = getattr(request.state, "client_ip", "unknown")
    if not _limiter.allow(client_ip, settings.health_rate_limit_per_minute):
        raise ApiProblem(
            429,
            TYPE_RATE_LIMIT,
            "Too many readiness probes",
            headers={"Retry-After": "60"},
        )

    cached = _cache.get()
    if cached is not None:
        return cached

    database = _check_database()
    redis_ok = _check_redis(settings)
    result = Readiness(
        status="ok" if database and redis_ok else "degraded",
        database="ok" if database else "fail",
        redis="ok" if redis_ok else "fail",
    )
    if settings.health_cache_seconds:
        _cache.put(result, settings.health_cache_seconds)
    return result
