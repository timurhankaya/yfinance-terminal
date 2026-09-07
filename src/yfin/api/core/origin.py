"""Where a request says it came from, and whether that is this site.

Two surfaces ask the question and they ask it for the same reason: the
browser attaches the visitor's ambient authority (the cached Basic
credential on `/admin`, the visitor's own network on `/ui/ws`) to a
request another page told it to make. Neither surface has a cookie, so
`SameSite` decides nothing for either, and `form-action 'self'` in the
admin CSP restricts where *our* forms may post -- not where someone
else's may post to us.

So the check lives here once, in `api/core` rather than in `ui/`: the
admin page must not import the optional UI package to get it.

Both answers are deliberately permissive for a request that is not a
browser at all. `curl`, `wscat` and the test client send neither
`Origin` nor `Sec-Fetch-Site`, and refusing those would break every
operator script while stopping no attack: a program that can set its own
headers is not the thing this defends against.
"""

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
    """The values a browser puts in `Sec-Fetch-Site`.

    All four are spelled out rather than just the one that is compared,
    because the comparison is a whitelist of one and the reader has to be
    able to see what it excludes.
    """

    SameOrigin = "same-origin"
    SameSite = "same-site"
    CrossSite = "cross-site"
    #: A request the user themselves started: a typed URL, a bookmark.
    UserInitiated = "none"


def origin_allowed(origin: str | None, host: str | None, settings: ApiSettings) -> bool:
    """Whether `origin` is this deployment.

    Scheme-independent on purpose. `public_base_url` is what the operator
    published and is authoritative when set; without it the request's own
    `Host` is the only thing that knows what this deployment is called,
    and a deployment behind a TLS-terminating proxy sees `http` on the
    inside while the browser sends `https`.
    """
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

    `Sec-Fetch-Site` is preferred where it exists because the browser
    computes it and a page cannot forge it; `Origin` is the fallback for
    the browsers that do not send fetch metadata, and it is compared the
    same way `/ui/ws` compares it.
    """
    site = conn.headers.get(HEADER_FETCH_SITE)
    if site is not None:
        return site == FetchSite.SameOrigin
    origin = conn.headers.get(HEADER_ORIGIN)
    if origin is None:
        return True
    return origin_allowed(origin, conn.headers.get(HEADER_HOST), settings)
