"""HTTP Basic auth for the admin page.

One operator, one secret, in the environment. The browser keeps the
credential for the tab's lifetime and sends it with every request, so
there is no session to issue, refresh or revoke. What there is: a
constant-time comparison, and a per-address window on failures so a
guess costs a minute after five misses.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from yfin.api.core.config import ApiSettings
from yfin.api.core.errors import TYPE_RATE_LIMIT, TYPE_UNAUTHENTICATED, ApiProblem
from yfin.api.ratelimit.fixed_window import FixedWindow

REALM = "yfin admin"
#: Failed attempts per client IP per minute, per process.
FAILURES_PER_MINUTE = 5

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
    if credentials is None:
        raise _challenge()
    expected = settings.admin_password.encode()
    given = credentials.password.encode()
    # The window is charged before the comparison so a burst of wrong
    # guesses is refused with 429 rather than costing a compare each.
    if not _failures.allow(client_ip, FAILURES_PER_MINUTE):
        raise ApiProblem(
            429,
            TYPE_RATE_LIMIT,
            "Too many failed admin logins from this address",
            headers={"Retry-After": "60"},
        )
    if not expected or not hmac.compare_digest(expected, given):
        raise _challenge()


AdminDep = Annotated[None, Depends(require_admin)]
