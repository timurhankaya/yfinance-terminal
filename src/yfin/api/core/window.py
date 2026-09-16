"""The per-IP cap for the unauthenticated endpoints (`/health/ready`,
`/metrics`). In-process, so a readiness probe can report Redis down
without needing Redis; hence per worker, which is fine for a limit that
bounds damage rather than bills. Each endpoint gets its own window so a
scraper cannot eat a probe's budget."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Request

from yfin.api.core.config import ApiSettings
from yfin.api.core.errors import TYPE_RATE_LIMIT, ApiProblem
from yfin.api.ratelimit.fixed_window import FixedWindow

#: One window per endpoint name. Module state, like the limiter it wraps:
#: the counters have to survive between requests, and there is one of these
#: per worker process by design.
_windows: dict[str, FixedWindow] = {}


def allow(name: str, client_ip: str, limit: int) -> bool:
    """Whether this IP may call `name` again in the current minute."""
    window = _windows.get(name)
    if window is None:
        window = _windows.setdefault(name, FixedWindow())
    return window.allow(client_ip, limit)


def check(name: str, request: Request) -> None:
    """Raises 429 when the caller is over the limit for `name`.

    `Retry-After: 60` because the window is a fixed minute: telling a
    caller to come back sooner would only earn it another refusal.
    """
    settings: ApiSettings = request.app.state.api_settings
    client_ip = getattr(request.state, "client_ip", "unknown")
    if not allow(name, client_ip, settings.health_rate_limit_per_minute):
        raise ApiProblem(
            429,
            TYPE_RATE_LIMIT,
            f"Too many {name} requests",
            headers={"Retry-After": "60"},
        )


def guard(name: str) -> Callable[[Request], None]:
    """The same check as a FastAPI dependency, for a route we do not write:
    `/metrics` is mounted by the instrumentator, so a cap can only go in
    `dependencies=[...]`."""

    def dependency(request: Request) -> None:
        check(name, request)

    dependency.__name__ = f"{name}_window"
    return dependency


metrics_window = guard("metrics")
