"""Server-rendered HTML for the admin page: one layout, a handful of
helpers, no template engine. Every dynamic string passes through `esc`.
Styles are a separate stylesheet route so the page can carry a CSP with
no inline allowance."""

from __future__ import annotations

from html import escape
from typing import Any

from fastapi.responses import HTMLResponse

CSP = (
    "default-src 'none'; style-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'"
)

HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}

NAV: tuple[tuple[str, str], ...] = (
    ("settings", "Settings"),
    ("proxies", "Proxies"),
    ("screens", "Screens"),
    ("clients", "API clients"),
)

CSS = """
:root {
  color-scheme: dark;
  --bg: #0b0e11;
  --fg: #d7dde3;
  --muted: #7f8a96;
  --line: #1f262e;
  --accent: #f2b544;
  --bad: #ff6b6b;
  --good: #6bd38a;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--fg);
  font: 13px/1.45 ui-monospace, "SF Mono", Menlo, monospace;
}
header {
  display: flex;
  gap: 18px;
  align-items: baseline;
  padding: 10px 16px;
  border-bottom: 1px solid var(--line);
}
header h1 { margin: 0; font-size: 14px; color: var(--accent); }
header nav a { color: var(--muted); text-decoration: none; margin-right: 12px; }
header nav a.active, header nav a:hover { color: var(--fg); }
main { padding: 12px 16px 40px; max-width: 1400px; }
h2 { font-size: 14px; margin: 18px 0 8px; }
p.note { color: var(--muted); max-width: 90ch; }
p.flash { padding: 8px 10px; border: 1px solid var(--line); }
p.flash.ok { border-color: var(--good); color: var(--good); }
p.flash.error { border-color: var(--bad); color: var(--bad); }
table { border-collapse: collapse; width: 100%; }
th {
  text-align: left;
  color: var(--muted);
  font-weight: 400;
  border-bottom: 1px solid var(--line);
  padding: 5px 8px;
  white-space: nowrap;
}
td { padding: 5px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
td.muted, span.muted { color: var(--muted); }
form.inline { display: inline-flex; gap: 6px; align-items: center; margin: 0; }
input, select {
  font: inherit;
  padding: 3px 6px;
  background: var(--bg);
  color: var(--fg);
  border: 1px solid var(--line);
}
input[type=text] { min-width: 14ch; }
button {
  font: inherit;
  padding: 3px 9px;
  background: var(--line);
  color: var(--fg);
  border: 1px solid var(--line);
  cursor: pointer;
}
button.primary { background: var(--accent); color: #000; border-color: var(--accent); }
button.danger { color: var(--bad); }
.source-db { color: var(--accent); }
.source-env { color: var(--good); }
.health-dead, .off { color: var(--bad); }
.health-healthy, .on { color: var(--good); }
.health-cooldown { color: var(--accent); }
code { color: var(--accent); }
"""


def esc(value: Any) -> str:
    """Anything to safe text; None shows as a dash."""
    if value is None:
        return "—"
    return escape(str(value), quote=True)


def page(
    title: str, active: str, body: str, *, ok: str | None = None, error: str | None = None
) -> HTMLResponse:
    nav = "".join(
        f'<a href="/admin/{key}" class="{"active" if key == active else ""}">{esc(label)}</a>'
        for key, label in NAV
    )
    flash = ""
    if error:
        flash = f'<p class="flash error">{esc(error)}</p>'
    elif ok:
        flash = f'<p class="flash ok">{esc(ok)}</p>'
    html = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        f"<title>{esc(title)} · yfin admin</title>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        '<link rel=stylesheet href="/admin/admin.css"></head><body>'
        f"<header><h1>yfin admin</h1><nav>{nav}</nav>"
        '<span class=muted><a href="/ui">terminal</a></span></header>'
        f"<main>{flash}{body}</main></body></html>"
    )
    return HTMLResponse(html, headers=HEADERS)


def button_form(action: str, label: str, *, kind: str = "", confirm: bool = False) -> str:
    """A one-button POST form. `confirm` is a plain text hint, not a
    script: the page ships no JavaScript."""
    cls = f' class="{kind}"' if kind else ""
    title = ' title="This cannot be undone"' if confirm else ""
    return (
        f'<form class=inline method=post action="{esc(action)}">'
        f"<button{cls}{title}>{esc(label)}</button></form>"
    )


def yes_no(value: bool) -> str:
    return "<span class=on>yes</span>" if value else "<span class=off>no</span>"
