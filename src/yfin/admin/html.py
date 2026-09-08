"""Server-rendered HTML for the admin page: one layout, a handful of
helpers, no template engine. Every dynamic string passes through `esc`.
Styles are a separate stylesheet route so the page can carry a CSP with
no inline allowance.

The look is the terminal's quieter sibling. The terminal shows live
prices and earns its brightness; this page shows settings that are
touched once a month, so colour is spent on one thing only: which value
no longer matches its default. Amber means CHANGED here, never "click
me" -- an operator scanning seventy rows needs the eye pulled to the
three that someone edited, not to seventy identical Save buttons.

Two typefaces, split by job. Keys, values and identifiers are monospace
because they are compared character by character; prose is the system
sans, because a paragraph explaining what `yf_stream_publish_enabled`
does is read, not compared.
"""

from __future__ import annotations

import enum
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
  --ink: #0b0f13;
  --raise: #141a21;
  --line: #212b34;
  --fg: #e3e9ef;
  --muted: #8d9aa8;
  --amber: #f0b429;
  --good: #5fd39a;
  --bad: #ff7a7a;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  --mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, monospace;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--ink);
  color: var(--fg);
  font: 400 14px/1.5 var(--sans);
  -webkit-font-smoothing: antialiased;
}
code, .mono { font-family: var(--mono); font-size: 0.92em; }

/* --- chrome ------------------------------------------------------- */
header {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 24px;
  align-items: center;
  padding: 12px 24px;
  border-bottom: 1px solid var(--line);
  background: var(--raise);
}
header .brand {
  font-family: var(--mono);
  font-size: 13px;
  letter-spacing: 0.02em;
  color: var(--amber);
  text-decoration: none;
}
header nav { display: flex; gap: 4px; flex: 1; flex-wrap: wrap; }
header nav a {
  color: var(--muted);
  text-decoration: none;
  padding: 4px 10px;
  border-radius: 4px;
}
header nav a:hover { color: var(--fg); background: #1b232b; }
header nav a.active { color: var(--ink); background: var(--fg); font-weight: 600; }
header .exit { color: var(--muted); text-decoration: none; font-size: 13px; }
header .exit:hover { color: var(--fg); }
main { padding: 28px 24px 72px; max-width: 1500px; }

/* --- page head ---------------------------------------------------- */
h2 {
  font-size: 24px;
  font-weight: 600;
  letter-spacing: -0.015em;
  margin: 0 0 8px;
}
p.note {
  color: var(--muted);
  max-width: 74ch;
  margin: 0 0 10px;
}
p.note:last-of-type { margin-bottom: 24px; }
p.note code { color: var(--fg); }

/* --- flash -------------------------------------------------------- */
p.flash {
  padding: 10px 14px;
  margin: 0 0 20px;
  border-left: 3px solid var(--line);
  background: var(--raise);
  border-radius: 0 4px 4px 0;
}
p.flash.ok { border-left-color: var(--good); }
p.flash.error { border-left-color: var(--bad); }

/* --- summary of what was changed ---------------------------------- */
.changed-summary {
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 14px 16px;
  margin: 0 0 28px;
  background: var(--raise);
}
.changed-summary h3 { margin: 0 0 8px; font-size: 15px; font-weight: 600; }
.changed-summary ul {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.changed-summary li a {
  display: inline-block;
  font-family: var(--mono);
  font-size: 12px;
  color: var(--amber);
  text-decoration: none;
  border: 1px solid #3a3222;
  border-radius: 4px;
  padding: 3px 8px;
  background: #1c1810;
}
.changed-summary li a:hover { border-color: var(--amber); }
.changed-summary p { margin: 0; color: var(--muted); }

/* --- group sections ----------------------------------------------- */
section.group { margin: 0 0 34px; }
section.group h3 {
  font-size: 15px;
  font-weight: 600;
  margin: 0 0 2px;
  scroll-margin-top: 12px;
}
section.group h3 .count { color: var(--muted); font-weight: 400; font-size: 13px; }
.jump { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 26px; }
.jump a {
  font-size: 13px;
  color: var(--muted);
  text-decoration: none;
  border: 1px solid var(--line);
  border-radius: 4px;
  padding: 3px 9px;
}
.jump a:hover { color: var(--fg); border-color: var(--muted); }

/* --- tables ------------------------------------------------------- */
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; margin-top: 8px; }
/* Every group is its own table, so without a shared column geometry the
   columns would step sideways from section to section and the eye would
   lose the vertical line it reads down. */
table.settings { table-layout: fixed; min-width: 940px; }
.c-key { width: 34%; }
.c-type { width: 10%; }
.c-default { width: 14%; }
.c-source { width: 11%; }
.c-value { width: 31%; }
table.settings td { overflow-wrap: anywhere; }
th {
  text-align: left;
  color: var(--muted);
  font-weight: 500;
  font-size: 12px;
  border-bottom: 1px solid var(--line);
  padding: 6px 10px;
  white-space: nowrap;
  background: var(--ink);
  position: sticky;
  top: 0;
}
td {
  padding: 9px 10px;
  border-bottom: 1px solid #171e25;
  vertical-align: top;
}
tbody tr { border-left: 2px solid transparent; }
tbody tr:hover { background: #10161c; }
tbody tr.changed { border-left-color: var(--amber); }
td.num { text-align: right; font-family: var(--mono); font-variant-numeric: tabular-nums; }
td.muted, span.muted { color: var(--muted); }
td.key code { color: var(--fg); font-size: 13px; }
p.desc { margin: 3px 0 0; color: var(--muted); font-size: 13px; max-width: 46ch; }
tr:target { background: #1a1710; }

/* --- forms -------------------------------------------------------- */
form.inline { display: inline-flex; gap: 6px; align-items: center; margin: 0; flex-wrap: wrap; }
.add-proxy {
  display: flex;
  gap: 10px;
  align-items: flex-end;
  flex-wrap: wrap;
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 14px 16px;
  margin: 0 0 10px;
  background: var(--raise);
}
/* The fields share the row's width instead of asserting their own: a
   `size` wide enough to show a proxy DSN would otherwise push past the
   card's edge on a phone. */
.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex: 1 1 200px;
  max-width: 260px;
}
.add-proxy .field:first-child { flex: 2 1 260px; max-width: 560px; }
.add-proxy input { width: 100%; }
.field label { font-size: 12px; color: var(--muted); }
input, select {
  font: 400 13px/1.4 var(--mono);
  padding: 6px 8px;
  background: var(--ink);
  color: var(--fg);
  border: 1px solid var(--line);
  border-radius: 4px;
}
input:hover { border-color: #2c3742; }
input[type=text] { min-width: 15ch; }
input::placeholder { color: #5d6975; }
button {
  font: 500 13px/1.4 var(--sans);
  padding: 6px 12px;
  background: transparent;
  color: var(--fg);
  border: 1px solid var(--line);
  border-radius: 4px;
  cursor: pointer;
}
button:hover { background: #1b232b; border-color: #33404c; }
button.primary {
  background: var(--amber);
  color: #17130a;
  border-color: var(--amber);
  font-weight: 600;
}
button.primary:hover { background: #ffc540; border-color: #ffc540; }
button.danger { color: var(--bad); }
button.danger:hover { background: #2a1618; border-color: var(--bad); }
:focus-visible { outline: 2px solid var(--amber); outline-offset: 2px; }

/* --- state -------------------------------------------------------- */
.source-db { color: var(--amber); }
.source-env { color: var(--good); }
.source-default { color: var(--muted); }
.health-dead, .off { color: var(--bad); }
.health-healthy, .on { color: var(--good); }
.health-cooldown { color: var(--amber); }
.health-unknown { color: var(--muted); }
.empty {
  padding: 26px 20px;
  margin-top: 8px;
  color: var(--muted);
  border: 1px dashed var(--line);
  border-radius: 6px;
}
.empty strong { display: block; color: var(--fg); font-weight: 600; margin-bottom: 4px; }

@media (max-width: 700px) {
  main { padding: 20px 14px 60px; }
  header { padding: 10px 14px; gap: 10px 16px; }
  /* The nav drops to its own full-width row rather than being squeezed
     into the middle column, which would stack the four links vertically
     and strand the wordmark beside them. */
  header nav { order: 3; flex: 1 1 100%; }
  header .brand { flex: 1; }
  h2 { font-size: 20px; }
}
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
        '<header><a class=brand href="/admin/">yfin admin</a>'
        f"<nav>{nav}</nav>"
        '<a class=exit href="/ui">Back to terminal</a></header>'
        f"<main>{flash}{body}</main></body></html>"
    )
    return HTMLResponse(html, headers=HEADERS)


class ButtonKind(enum.StrEnum):
    """The button's CSS class; PLAIN has none."""

    PLAIN = ""
    PRIMARY = "primary"
    DANGER = "danger"


def button_form(
    action: str,
    label: str,
    *,
    kind: ButtonKind = ButtonKind.PLAIN,
    confirm: bool = False,
    describes: str = "",
) -> str:
    """A one-button POST form. `confirm` is a plain text hint, not a
    script: the page ships no JavaScript.

    `describes` names what the button acts on. Seventy buttons all
    labelled "Save" are seventy identical stops in a screen reader, so
    the accessible name carries the row's subject even though the
    visible label stays short.
    """
    cls = f' class="{kind.value}"' if kind is not ButtonKind.PLAIN else ""
    title = ' title="This cannot be undone"' if confirm else ""
    aria = f' aria-label="{esc(label)} {esc(describes)}"' if describes else ""
    return (
        f'<form class=inline method=post action="{esc(action)}">'
        f"<button{cls}{title}{aria}>{esc(label)}</button></form>"
    )


def yes_no(value: bool) -> str:
    return "<span class=on>yes</span>" if value else "<span class=off>no</span>"


def empty(headline: str, hint: str) -> str:
    """What a table says when it has no rows. An empty screen is an
    invitation to act, so it names the next step rather than reporting
    a count of zero."""
    return f"<div class=empty><strong>{esc(headline)}</strong>{esc(hint)}</div>"
