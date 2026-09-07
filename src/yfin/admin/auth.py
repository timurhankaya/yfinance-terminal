"""HTTP Basic auth for the admin page.

One operator, one secret, in the environment. The browser keeps the
credential for the tab's lifetime and sends it with every request, so
there is no session to issue, refresh or revoke. What there is: a
constant-time comparison, and a per-address window on failures so a
guess costs a minute after five misses.

That same automatic credential is why this dependency also answers a
second question. Basic auth establishes WHO; nothing in a cross-site
form POST establishes that the operator asked for it, and the browser
would attach the cached credential to it anyway (there is no cookie, so
no `SameSite` applies). Both questions are answered in the one
dependency every write route already shares, so a new route cannot
acquire one guard and miss the other.
"""

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

#: The problem type for a write that did not come from this site.
#:
#: Declared here rather than in `api/core/errors.py`: `ALL_TYPES` there
#: is the enum the published `openapi.json` carries, and the admin page
#: is deliberately outside that document. A type only /admin can emit
#: does not belong in the contract clients read.
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
