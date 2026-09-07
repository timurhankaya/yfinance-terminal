"""The admin routes. Forms POST, the server acts, then redirects back to
the page with the outcome in the query string (`?ok=` / `?error=`), so a
reload never repeats a write. Nothing here is in the OpenAPI document."""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from yfin.admin import ops
from yfin.admin.auth import AdminDep
from yfin.admin.html import CSS, ButtonKind, button_form, esc, page, yes_no
from yfin.admin.ops import ProxyAction, ScreenAction
from yfin.api.core.errors import TYPE_NOT_FOUND, ApiProblem
from yfin.api.storage.session import session_scope

router = APIRouter(prefix="/admin", include_in_schema=False)
SessionDep = Annotated[Session, Depends(session_scope)]
Flash = Annotated[str | None, Query(max_length=500)]


def back(to: str, *, ok: str | None = None, error: str | None = None) -> RedirectResponse:
    query = urlencode({k: v for k, v in (("ok", ok), ("error", error)) if v})
    return RedirectResponse(f"/admin/{to}{'?' + query if query else ''}", status_code=303)


@router.get("/admin.css")
def stylesheet() -> Response:
    return Response(CSS, media_type="text/css", headers={"Cache-Control": "no-store"})


@router.get("")
@router.get("/")
def root(_: AdminDep) -> RedirectResponse:
    return RedirectResponse("/admin/settings", status_code=302)


# --- settings ---------------------------------------------------------------


def _bound(value: float) -> float | int:
    """`ge=1` reads as 1, not 1.0."""
    return int(value) if value == int(value) else value


@router.get("/settings")
def settings_page(_: AdminDep, ok: Flash = None, error: Flash = None) -> Response:
    rows = []
    for view in ops.list_settings():
        s, st = view.schema, view.state
        bounds = ""
        if s.min is not None or s.max is not None:
            low = esc(_bound(s.min)) if s.min is not None else ""
            high = esc(_bound(s.max)) if s.max is not None else ""
            bounds = f" [{low}..{high}]"
        unset = (
            button_form(f"/admin/settings/{s.key}/unset", "Unset", kind=ButtonKind.DANGER)
            if st.has_row
            else ""
        )
        rows.append(
            "<tr>"
            f"<td><code>{esc(s.key)}</code><br><span class=muted>{esc(s.description)}</span></td>"
            f"<td class=muted>{esc(s.group)}</td>"
            f"<td class=muted>{esc(s.type)}{bounds}</td>"
            f"<td class=muted>{esc(view.default)}</td>"
            f'<td><span class="source-{esc(st.source.value)}">{esc(st.source.value)}</span></td>'
            f'<td><form class=inline method=post action="/admin/settings/{esc(s.key)}">'
            f'<input type=text name=value value="{esc(st.value)}" aria-label="{esc(s.key)}">'
            f"<button class=primary>Save</button></form> {unset}</td>"
            "</tr>"
        )
    body = (
        "<h2>Settings</h2>"
        "<p class=note>The pipeline's DB-managed settings, the same ones <code>yfin config</code> "
        "edits. Save writes a row in the <code>settings</code> table after validating the value "
        "against the model; Unset deletes the row so the value falls back to <code>.env</code>, "
        "then to the default. Running processes read the table at start.</p>"
        "<p class=note>At start means exactly that: the table is never re-read. A change here "
        "reaches the next <code>yfin sync</code> by itself, but the keys the API process reads "
        "need the API restarted before they take effect. "
        "<code>yf_stream_publish_enabled</code> and <code>yf_stream_publish_redis_url</code> are "
        "the ones to watch: they decide whether the terminal's live feed runs at all, and until "
        "the restart every open page is still told the feed is off.</p>"
        "<table><thead><tr><th>key</th><th>group</th><th>type</th><th>default</th><th>source</th>"
        "<th>effective value</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )
    return page("Settings", "settings", body, ok=ok, error=error)


@router.post("/settings/{key}")
def settings_save(_: AdminDep, key: str, value: Annotated[str, Form()]) -> RedirectResponse:
    try:
        canonical = ops.save_setting(key, value)
    except ops.AdminError as exc:
        return back("settings", error=f"{key}: {exc}")
    return back("settings", ok=f"{canonical} saved")


@router.post("/settings/{key}/unset")
def settings_unset(_: AdminDep, key: str) -> RedirectResponse:
    removed = ops.clear_setting(key)
    return back("settings", ok=f"{key} {'unset' if removed else 'had no row'}")


# --- proxies ----------------------------------------------------------------


@router.get("/proxies")
def proxies_page(
    _: AdminDep, session: SessionDep, ok: Flash = None, error: Flash = None
) -> Response:
    rows = []
    for p in ops.list_proxies(session):
        actions = " ".join(
            [
                button_form(f"/admin/proxies/{p.id}/{ProxyAction.DISABLE}", "Disable")
                if p.is_enabled
                else button_form(f"/admin/proxies/{p.id}/{ProxyAction.ENABLE}", "Enable"),
                button_form(f"/admin/proxies/{p.id}/{ProxyAction.RESET}", "Reset health"),
                button_form(
                    f"/admin/proxies/{p.id}/{ProxyAction.REMOVE}",
                    "Remove",
                    kind=ButtonKind.DANGER,
                    confirm=True,
                ),
            ]
        )
        rows.append(
            "<tr>"
            f"<td>{esc(p.label)}</td>"
            f"<td>{esc(p.scheme.value)}://{esc(p.host)}:{esc(p.port)}</td>"
            f"<td>{esc(p.username or '')}{' · secret' if p.password_enc else ''}</td>"
            f"<td>{yes_no(p.is_enabled)}</td>"
            f'<td><span class="health-{esc(p.health.value)}">{esc(p.health.value)}</span></td>'
            f"<td class=num>{esc(p.success_count)}</td><td class=num>{esc(p.failure_count)}</td>"
            f"<td class=num>{esc(p.consecutive_failures)}</td>"
            f"<td class=num>{esc(p.last_latency_ms)}</td>"
            f"<td class=muted>{esc(p.cooldown_until)}</td>"
            f"<td class=muted>{esc(p.last_error)}</td>"
            f"<td>{actions}</td>"
            "</tr>"
        )
    body = (
        "<h2>Proxies</h2>"
        "<p class=note>The pool <code>yfin sync</code> draws from. Health is written by the "
        "pipeline; Enable/Disable is the operator's decision and does not touch it. Reset "
        "clears health, cooldown and the consecutive-failure counter (a dead proxy comes back); "
        "the cumulative counters stay. Remove deletes the row.</p>"
        '<form method=post action="/admin/proxies" class=inline>'
        '<input type=text name=url placeholder="http://user:secret@host:port" size=48 '
        'aria-label="proxy url" required>'
        '<input type=text name=label placeholder="label (optional)" aria-label="label">'
        "<button class=primary>Add</button></form>"
        "<p class=note>The URL form is the one <code>yfin proxy add</code> takes; the secret is "
        "stored Fernet-encrypted under <code>YF_PROXY_SECRET_KEY</code> and never shown again.</p>"
        "<table><thead><tr><th>label</th><th>endpoint</th><th>user</th><th>enabled</th>"
        "<th>health</th><th>ok</th><th>fail</th><th>consec.</th><th>ms</th><th>cooldown until</th>"
        "<th>last error</th><th></th></tr></thead>"
        "<tbody>"
        + ("".join(rows) or "<tr><td colspan=12 class=muted>No proxies.</td></tr>")
        + "</tbody></table>"
    )
    return page("Proxies", "proxies", body, ok=ok, error=error)


@router.post("/proxies")
def proxies_add(
    _: AdminDep,
    session: SessionDep,
    url: Annotated[str, Form(max_length=500)],
    label: Annotated[str, Form(max_length=100)] = "",
) -> RedirectResponse:
    try:
        row = ops.add_proxy(session, url.strip(), label.strip() or None)
    except ops.AdminError as exc:
        return back("proxies", error=str(exc))
    return back("proxies", ok=f"added {row.label}")


@router.post("/proxies/{proxy_id}/{action}")
def proxies_act(_: AdminDep, session: SessionDep, proxy_id: int, action: str) -> RedirectResponse:
    try:
        verb = ProxyAction(action)
    except ValueError:
        raise ApiProblem(404, TYPE_NOT_FOUND, "No such action") from None
    try:
        label = ops.proxy_action(session, proxy_id, verb)
    except ops.AdminError as exc:
        return back("proxies", error=str(exc))
    return back("proxies", ok=f"{action}: {label}")


# --- screens ----------------------------------------------------------------


@router.get("/screens")
def screens_page(
    _: AdminDep, session: SessionDep, ok: Flash = None, error: Flash = None
) -> Response:
    rows = []
    for s in ops.list_screens(session):
        toggle = (
            button_form(f"/admin/screens/{s.screen_key}/{ScreenAction.DISABLE}", "Disable")
            if s.is_enabled
            else button_form(f"/admin/screens/{s.screen_key}/{ScreenAction.ENABLE}", "Enable")
        )
        rows.append(
            "<tr>"
            f"<td><code>{esc(s.screen_key)}</code></td><td>{esc(s.title)}</td>"
            f"<td class=muted>{esc(s.kind.value)} · {esc(s.quote_type.value)}</td>"
            f"<td class=muted>{esc(s.description)}</td>"
            f"<td>{yes_no(s.is_enabled)}</td><td>{toggle}</td>"
            "</tr>"
        )
    body = (
        "<h2>Screens</h2>"
        "<p class=note>Which Yahoo screens <code>yfin sync --datasets screener</code> runs. "
        "The definition lives in <code>screens.py</code>; this switch is the runtime decision "
        "and the file does not turn a disabled screen back on.</p>"
        "<table><thead><tr><th>key</th><th>title</th><th>kind</th><th>description</th>"
        "<th>enabled</th><th></th></tr></thead>"
        "<tbody>"
        + ("".join(rows) or "<tr><td colspan=6 class=muted>No screens seeded yet.</td></tr>")
        + "</tbody></table>"
    )
    return page("Screens", "screens", body, ok=ok, error=error)


@router.post("/screens/{screen_key}/{action}")
def screens_act(_: AdminDep, session: SessionDep, screen_key: str, action: str) -> RedirectResponse:
    try:
        verb = ScreenAction(action)
    except ValueError:
        raise ApiProblem(404, TYPE_NOT_FOUND, "No such action") from None
    try:
        ops.set_screen_enabled(session, screen_key, verb is ScreenAction.ENABLE)
    except ops.AdminError as exc:
        return back("screens", error=str(exc))
    return back("screens", ok=f"{action}: {screen_key}")


# --- API clients ------------------------------------------------------------


@router.get("/clients")
def clients_page(_: AdminDep, session: SessionDep) -> Response:
    rows = []
    for c in ops.list_clients(session):
        rows.append(
            "<tr>"
            f"<td><code>{esc(c.client_id)}</code></td><td>{esc(c.name)}</td>"
            f"<td>{esc(c.plan)}</td><td>{yes_no(c.is_active)}</td>"
            f"<td class=num>{esc(c.auth_epoch)}</td>"
            f"<td class=muted>{esc(c.last_used_at)}</td><td class=muted>{esc(c.created_at)}</td>"
            "</tr>"
        )
    body = (
        "<h2>API clients</h2>"
        "<p class=note>Read-only. Creating, rotating, revoking, scoping and disabling a client "
        "publishes a revocation to Redis so running workers drop its tokens; that path lives in "
        "<code>yfin api client</code> and is not duplicated here. Owner contact details are "
        "deliberately not shown.</p>"
        "<table><thead><tr><th>client</th><th>name</th><th>plan</th><th>active</th>"
        "<th>epoch</th><th>last used</th><th>created</th></tr></thead>"
        "<tbody>"
        + ("".join(rows) or "<tr><td colspan=7 class=muted>No clients.</td></tr>")
        + "</tbody></table>"
    )
    return page("API clients", "clients", body)
