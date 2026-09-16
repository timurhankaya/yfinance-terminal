"""HTTP Basic auth for the admin page, with a per-address brake on failures.
The browser attaches the cached credential to cross-site form POSTs too (no
cookie, so no `SameSite`), so the same dependency also enforces same-origin
on writes; a route cannot take one guard and miss the other."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from yfin.api.core.config import ApiSettings
from yfin.api.core.errors import TYPE_RATE_LIMIT, TYPE_UNAUTHENTICATED, ApiProblem
from yfin.api.core.origin import SAFE_METHODS, same_origin_write
from yfin.api.ratelimit.fixed_window import FixedWindow

REALM = "yfin admin"
#: Failed attempts per client IP per minute, per process.
FAILURES_PER_MINUTE = 5

#: Problem type for a write that did not come from this site. Kept out of
#: `api/core/errors.ALL_TYPES`: that enum is published in `openapi.json`
#: and /admin is outside that contract.
TYPE_CROSS_SITE = "cross_site_request"

_scheme = HTTPBasic(auto_error=False, realm=REALM)
_failures = FixedWindow()


def _challenge() -> ApiProblem:
    return ApiProblem(
        401,
        TYPE_UNAUTHENTICATED,
        "Admin credentials required",
        headers={"WWW-Authenticate": f'Basic realm="{REALM}"'},
    )


def require_admin(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials | None, Depends(_scheme)],
) -> None:
    """Passes silently or raises. The username is not checked: there is
    one operator and the secret is the whole credential."""
    settings: ApiSettings = request.app.state.api_settings
    client_ip = getattr(request.state, "client_ip", "unknown")
    # Before the credential, because this is not a login failure and must
    # not spend the operator's five attempts. A cross-site POST that also
    # carries no credential is refused for this reason rather than
    # challenged -- offering the challenge would be inviting the browser
    # to attach the credential to someone else's request.
    if request.method not in SAFE_METHODS and not same_origin_write(request, settings):
        raise ApiProblem(
            403,
            TYPE_CROSS_SITE,
            "This request did not come from the admin page",
            detail="a write to /admin must be submitted by the admin page itself",
        )
    if credentials is None:
        raise _challenge()
    # Read before the comparison, so a burst of wrong guesses is refused
    # with 429 rather than costing a compare each, and once the brake is
    # on it stays on for the window -- the right secret waits it out too.
    if _failures.over(client_ip, FAILURES_PER_MINUTE):
        raise ApiProblem(
            429,
            TYPE_RATE_LIMIT,
            "Too many failed admin logins from this address",
            headers={"Retry-After": "60"},
        )
    expected = settings.admin_password.encode()
    given = credentials.password.encode()
    if not expected or not hmac.compare_digest(expected, given):
        # Charged here and nowhere else: these are FAILURES per minute.
        # Counting the requests that succeed instead would lock the
        # operator out of their own page after five page loads.
        _failures.allow(client_ip, FAILURES_PER_MINUTE)
        raise _challenge()


AdminDep = Annotated[None, Depends(require_admin)]
