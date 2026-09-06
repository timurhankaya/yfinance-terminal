"""The single guard an endpoint declares.

`guard(family)` is what a route depends on, and one argument settles
three things that must never disagree: the scope required, the counter
the request is billed to, and the limits applied. They all derive from
the data family, so there is no way to advertise one scope, meter under
another, and enforce a third.

Refunds and usage counting need the final status code, which a
dependency cannot see, so they happen in `UsageMiddleware`. The
dependency leaves what it did in `request.state` and the middleware
finishes the job -- including releasing the concurrency slot, which must
happen even when the handler raised.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from fastapi import Request, Response, Security
from starlette.middleware.base import BaseHTTPMiddleware

from yfin.api.auth.dependencies import Principal, current_principal
from yfin.api.core.config import ApiSettings
from yfin.api.core.errors import (
    TYPE_CONCURRENCY,
    TYPE_QUOTA,
    TYPE_RATE_LIMIT,
    ApiProblem,
)
from yfin.api.ratelimit import concurrency, limiter, policy, usage
from yfin.core.families import DataFamily, scope_for
from yfin.core.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class LimitState:
    """What the middleware needs to finish what the dependency started."""

    client_id: str
    family: str
    holds_slot: bool = False
    degraded: bool = False
    #: Set once the request has been refunded or counted, so the two
    #: exit paths below cannot both charge it.
    accounted: bool = False
    headers: dict[str, str] = field(default_factory=dict)


def meter(
    request: Request, response: Response, principal: Principal, family: DataFamily
) -> None:
    """Applies the plan's limits and records what was done.

    Split out of `guard` because the generic dataset surface cannot name
    its family in a signature -- the family depends on which dataset was
    asked for. Both paths run this same function, so a request served
    generically is metered exactly like one served by a hand-written
    endpoint.
    """
    settings: ApiSettings = request.app.state.api_settings
    limits = policy.limits_for_client(principal.client_id)
    state = LimitState(client_id=principal.client_id, family=family.value)
    request.state.limits = state
    request.state.page_size_cap = limits.max_page_size

    verdict = limiter.consume(settings, principal.client_id, limits)
    state.degraded = verdict.degraded
    state.headers = limiter.headers(verdict)
    # Set on the success path here; the refusals below carry the same
    # headers on the problem response.
    response.headers.update(state.headers)

    if not verdict.allowed:
        if verdict.reason == limiter.REASON_QUOTA:
            raise ApiProblem(
                429,
                TYPE_QUOTA,
                "Monthly quota exhausted",
                detail="the plan's monthly request quota is used up",
                headers={"Retry-After": str(verdict.retry_after), **state.headers},
            )
        raise ApiProblem(
            429,
            TYPE_RATE_LIMIT,
            "Too many requests",
            detail="the request rate exceeds the plan's limit",
            headers={"Retry-After": str(verdict.retry_after), **state.headers},
        )

    slot = concurrency.acquire(settings, principal.client_id, limits.max_concurrency)
    if not slot.acquired:
        raise ApiProblem(
            429,
            TYPE_CONCURRENCY,
            "Too many concurrent requests",
            detail=f"at most {limits.max_concurrency} requests may be in flight",
            headers={"Retry-After": "1", **state.headers},
        )
    state.holds_slot = not slot.degraded


def guard(family: DataFamily) -> Callable[..., Principal]:
    """Requires the family's scope, then meters the request under it."""
    scope = scope_for(family)

    # Security() in a default rather than inside Annotated, and that is
    # forced: `from __future__ import annotations` turns the annotation
    # into a string that FastAPI re-evaluates against module globals,
    # where the closure's `scope` does not exist. The Annotated form
    # silently degrades into "principal is a query parameter" and every
    # request 422s.
    def dependency(
        request: Request,
        response: Response,
        principal: Principal = Security(  # noqa: B008 - see above
            current_principal, scopes=[scope]
        ),
    ) -> Principal:
        meter(request, response, principal, family)
        return principal

    return dependency


class UsageMiddleware(BaseHTTPMiddleware):
    """Releases the slot, refunds server errors, counts what was billable.

    A client must not pay for our 500, so a 5xx gives the quota unit back
    and is not counted. A refusal we made before doing any work (429) is
    not counted either. Client errors are: a malformed request still cost
    a round trip and is the caller's to fix.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        try:
            response = await call_next(request)
        except Exception:
            # Starlette's ServerErrorMiddleware sits OUTSIDE this one, so
            # an unhandled exception propagates through here and we never
            # see the 500 it eventually becomes. Accounting for it has to
            # happen on this path, or a server error would silently be
            # charged to the client.
            self._account(request, status=500)
            raise
        finally:
            self._release(request)

        self._account(request, status=response.status_code)
        return response

    @staticmethod
    def _release(request: Request) -> None:
        state: LimitState | None = getattr(request.state, "limits", None)
        settings: ApiSettings | None = getattr(request.app.state, "api_settings", None)
        if state is not None and settings is not None and state.holds_slot:
            concurrency.release(settings, state.client_id)
            # Guards against a double release if this ever runs twice.
            state.holds_slot = False

    @staticmethod
    def _account(request: Request, *, status: int) -> None:
        state: LimitState | None = getattr(request.state, "limits", None)
        settings: ApiSettings | None = getattr(request.app.state, "api_settings", None)
        if state is None or settings is None or state.accounted:
            return
        state.accounted = True

        if status >= 500:
            limiter.refund_quota(settings, state.client_id)
        elif status != 429:
            usage.record(
                settings,
                client_id=state.client_id,
                family=state.family,
                estimated=state.degraded,
            )
