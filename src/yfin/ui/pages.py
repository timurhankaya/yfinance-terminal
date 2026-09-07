"""index.html and the built assets, by hand.

FastAPI 0.141 has `app.frontend()`, and it is not used here for three
reasons that were checked, not guessed. Its fallback answers EVERY
unmatched request that accepts text/html with 200 index.html -- a typo
under /v1 would come back as a web page instead of the 404 problem the
contract promises. Its `check_dir` raises at import when the build is
absent, which is every unit test and every unbuilt checkout. And it
offers no hook to put a Content-Security-Policy on the one response
that needs it.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.responses import HTMLResponse
from starlette.staticfiles import StaticFiles

#: `connect-src 'self'` covers same-origin WebSocket in every current
#: browser; no `ws:` scheme is listed because that would allow any host.
CSP = (
    "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
    "style-src 'self'; frame-ancestors 'none'"
)

_PAGE_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Frame-Options": "DENY",
    # The shell is tiny and the assets it names are content-hashed; the
    # page itself must never be cached or a deploy leaves stale hashes.
    "Cache-Control": "no-store",
}


def default_dist_dir() -> Path:
    """Where the Vite build lands, inside the installed package."""
    return Path(str(resources.files("yfin.ui") / "static" / "dist"))


def install_pages(app: FastAPI, dist_dir: Path) -> None:
    index = (dist_dir / "index.html").read_text(encoding="utf-8")
    pages = APIRouter(include_in_schema=False)

    # `path` is unused but must be declared: FastAPI binds `{path:path}`
    # to it. On /ui and /ui/ the default applies (verified: no 422).
    def spa(path: str = "") -> HTMLResponse:
        return HTMLResponse(index, headers=_PAGE_HEADERS)

    pages.add_api_route("/ui", spa, methods=["GET"])
    pages.add_api_route("/ui/", spa, methods=["GET"])
    pages.add_api_route("/ui/t/{path:path}", spa, methods=["GET"])

    app.include_router(pages)
    app.mount("/ui/assets", StaticFiles(directory=dist_dir / "assets"), name="ui-assets")
