"""Whether a request came from this site, shared by `/admin` and `/ui/ws`
(neither has a cookie, so `SameSite` decides nothing). Lives in `api/core`
so the admin page need not import the optional UI package. Requests with
no `Origin`/`Sec-Fetch-Site` are allowed: a program that sets its own
headers is not what this defends against."""

from __future__ import annotations

from enum import StrEnum
from typing import Final
from urllib.parse import urlparse

from starlette.requests import HTTPConnection

from yfin.api.core.config import ApiSettings

HEADER_ORIGIN: Final = "origin"
HEADER_HOST: Final = "host"
HEADER_FETCH_SITE: Final = "sec-fetch-site"

#: Methods that change nothing, so nothing has to establish intent for
#: them. Everything else on the admin page is a form POST.
SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})


class FetchSite(StrEnum):
    """The values a browser puts in `Sec-Fetch-Site`. All four are spelled
    out so the whitelist of one shows what it excludes."""

    SameOrigin = "same-origin"
    SameSite = "same-site"
    CrossSite = "cross-site"
    #: A request the user themselves started: a typed URL, a bookmark.
    UserInitiated = "none"


def origin_allowed(origin: str | None, host: str | None, settings: ApiSettings) -> bool:
    """Whether `origin` is this deployment. Scheme-independent: behind a
    TLS-terminating proxy the inside sees `http` while the browser sends
    `https`. `public_base_url` is authoritative when set, else `Host`."""
    if not origin:
        # No Origin at all is not a browser. `wscat` and the test client
        # send none; a page always does.
        return True
    expected = settings.public_base_url or (f"//{host}" if host else "")
    if not expected:
        return False
    return urlparse(origin).netloc == urlparse(expected).netloc


def same_origin_write(conn: HTTPConnection, settings: ApiSettings) -> bool:
    """Whether a state-changing request demonstrably came from this site.
    `Sec-Fetch-Site` is preferred (a page cannot forge it); `Origin` is the
    fallback for browsers without fetch metadata."""
    site = conn.headers.get(HEADER_FETCH_SITE)
    if site is not None:
        return site == FetchSite.SameOrigin
    origin = conn.headers.get(HEADER_ORIGIN)
    if origin is None:
        return True
    return origin_allowed(origin, conn.headers.get(HEADER_HOST), settings)
