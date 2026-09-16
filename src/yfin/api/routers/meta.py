"""Liveness and readiness. `/health/ready` is unauthenticated and touches
PostgreSQL and Redis, so the result is cached for a few seconds and the
endpoint carries its own in-process per-IP limit (a probe that needed
Redis to report Redis down would be useless)."""

from __future__ import annotations

import threading
import time
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import text

from yfin.api.core import window
from yfin.api.core.config import ApiSettings
from yfin.api.core.openapi import contract
from yfin.core.logging_setup import get_logger
from yfin.core.metrics import inc

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
            hit = self._value is not None and time.monotonic() < self._expires
        # Counted here rather than at the call site: this is the only place
        # that knows whether the entry was still live, and a miss is what
        # turns a probe into two connections.
        inc("yfin_cache_ops_total", cache="readiness", result="hit" if hit else "miss")
        with self._lock:
            return self._value if hit else None

    def put(self, value: Readiness, ttl: float) -> None:
        with self._lock:
            self._value = value
            self._expires = time.monotonic() + ttl


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
        log.error("health_database_unreachable", exc_info=True)
        return False


def _check_redis(settings: ApiSettings) -> bool:
    try:
        import redis

        client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=1)
        client.ping()
        return True
    except Exception:  # noqa: BLE001 - same contract as the database probe
        log.error("health_redis_unreachable", exc_info=True)
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
    # The same code `/metrics` is capped with, in its own window: a
    # Prometheus scraping every fifteen seconds must not eat a Kubernetes
    # probe's budget.
    window.check("readiness", request)

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
