"""The per-IP cap the unauthenticated endpoints share.

Two endpoints answer without a token: `/health/ready` and `/metrics`. Both
are therefore the cheapest attack surface the API has, and both do real
work -- readiness touches PostgreSQL and Redis, `/metrics` renders every
series the process holds. Left uncapped, a few thousand requests a second
on either would take real traffic down with it.

The cap is IN-PROCESS, and that is the point rather than a shortcut. A
readiness probe that needed Redis in order to report that Redis is down
would be useless exactly when it matters, and a `/metrics` endpoint that
needed Redis to answer would go blind in the outage a scrape is there to
show. It also means the limit is per worker: four uvicorn workers allow
four times the configured rate between them, which is the right answer for
a limit whose job is to bound the damage rather than to bill anyone.

Each endpoint gets its OWN window. Sharing one would let a Prometheus
scraping every fifteen seconds eat a Kubernetes probe's budget, and the
two failures would be indistinguishable.
"""

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
    """The same check as a FastAPI dependency, for a route we do not write.

    `/metrics` is mounted by the instrumentator, so there is no handler
    body to put the call in; `dependencies=[Depends(metrics_window)]` is
    the only place a cap can go.
    """

    def dependency(request: Request) -> None:
        check(name, request)

    dependency.__name__ = f"{name}_window"
    return dependency


metrics_window = guard("metrics")


def reset() -> None:
    """Empties every window. For tests, which must not inherit a minute."""
    _windows.clear()


__all__ = ["allow", "check", "guard", "metrics_window", "reset"]
