"""The UI's own endpoints: login, logout, who-am-I, and a 404 for
everything else under /ui/api so the SPA fallback can never answer an
API path with HTML.

None of this is in the OpenAPI document. The public contract is `/v1`
and `/oauth`; these routes exist for one browser page and are versioned
with it.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, Response
from pydantic import BaseModel

from yfin.api.core.config import ApiSettings
from yfin.api.core.errors import (
    TYPE_INVALID_TOKEN,
    TYPE_NOT_FOUND,
    TYPE_RATE_LIMIT,
    TYPE_UNAUTHENTICATED,
    ApiProblem,
)
from yfin.api.core.window import FixedWindow
from yfin.ui import session
from yfin.ui.session import COOKIE_NAME, SESSION_TTL_SECONDS, SessionInvalid, UiClaims

router = APIRouter(prefix="/ui/api", include_in_schema=False)

#: Per client IP, per process. Behind a reverse proxy this needs
#: YFAPI_TRUSTED_PROXIES, or every login in the world shares one bucket.
LOGIN_ATTEMPTS_PER_MINUTE = 5
_login_limiter = FixedWindow()

#: The form field the login page posts.
LOGIN_FIELD = "password"


class Me(BaseModel):
    authenticated: bool
    expires_at: int | None
    #: Filled in by 1c (the live tick path). Always false until then.
    live_enabled: bool


def _settings(request: Request) -> ApiSettings:
    settings: ApiSettings = request.app.state.api_settings
    return settings


def _claims_from_cookie(request: Request) -> UiClaims | None:
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return None
    try:
        return session.verify(_settings(request), raw)
    except SessionInvalid:
        return None


def current_session(request: Request) -> UiClaims:
    """The dependency /ui/api routes and the UI-only data routes use.

    Cookie only. A Bearer token is not a session, and accepting one here
    would let an API client reach UI-only routes that bypass metering.
    """
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        raise ApiProblem(401, TYPE_UNAUTHENTICATED, "Login required")
    try:
        return session.verify(_settings(request), raw)
    except SessionInvalid as exc:
        raise ApiProblem(401, TYPE_INVALID_TOKEN, "The session is not valid") from exc


UiSession = Annotated[UiClaims, Depends(current_session)]


def set_session_cookie(response: Response, settings: ApiSettings, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_TTL_SECONDS,
        path="/",
        secure=settings.ui_cookie_secure(),
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response, settings: ApiSettings) -> None:
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        secure=settings.ui_cookie_secure(),
        httponly=True,
        samesite="lax",
    )


def _matches(submitted: str, configured: str) -> bool:
    """Constant time, so a wrong first character costs the same as a
    wrong last one."""
    return hmac.compare_digest(submitted.encode("utf-8"), configured.encode("utf-8"))


@router.post("/login", status_code=204)
def login(
    request: Request,
    response: Response,
    submitted: Annotated[str, Form(alias=LOGIN_FIELD)],
) -> Response:
    settings = _settings(request)
    client_ip = getattr(request.state, "client_ip", "unknown")
    if not _login_limiter.allow(client_ip, LOGIN_ATTEMPTS_PER_MINUTE):
        raise ApiProblem(
            429, TYPE_RATE_LIMIT, "Too many login attempts", headers={"Retry-After": "60"}
        )
    if not _matches(submitted, settings.ui_password):
        raise ApiProblem(401, TYPE_UNAUTHENTICATED, "Wrong password")

    token, _ = session.issue(settings)
    response.status_code = 204
    set_session_cookie(response, settings, token)
    return response


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response) -> Response:
    response.status_code = 204
    clear_session_cookie(response, _settings(request))
    return response


@router.get("/me", response_model=Me)
def me(request: Request) -> Me:
    """Always 200: the SPA asks this first and a 401 here would be
    indistinguishable from an expired session mid-use."""
    claims = _claims_from_cookie(request)
    if claims is None:
        return Me(authenticated=False, expires_at=None, live_enabled=False)
    return Me(authenticated=True, expires_at=claims.expires_at, live_enabled=False)


@router.api_route(
    "/{path:path}", methods=["GET", "HEAD", "OPTIONS", "POST", "PUT", "DELETE", "PATCH"]
)
def not_found(path: str) -> None:
    """Registered last in this router, and this router before the SPA
    pages, so an unknown /ui/api path is a problem document, never
    index.html."""
    raise ApiProblem(404, TYPE_NOT_FOUND, "No such route")
