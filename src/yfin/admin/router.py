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
from yfin.admin.html import CSS, ButtonKind, button_form, empty, esc, page, yes_no
from yfin.admin.ops import ProxyAction, ScreenAction
from yfin.api.core.errors import TYPE_NOT_FOUND, ApiProblem
from yfin.api.storage.session import session_scope
from yfin.core.config import FieldSchema
from yfin.storage.settings_store import Source

router = APIRouter(prefix="/admin", include_in_schema=False)
SessionDep = Annotated[Session, Depends(session_scope)]
Flash = Annotated[str | None, Query(max_length=500)]

#: What each source is called on the page. The enum's own values are the
#: storage layer's vocabulary (`db`, `env`); an operator reading a table
#: wants to know WHERE the value came from, and ".env file" says that
#: while "env" could as easily mean the process environment.
SOURCE_LABEL: dict[Source, str] = {
    Source.DB: "database",
    Source.ENV: ".env file",
    Source.DEFAULT: "default",
}


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


def _bounds(schema: FieldSchema) -> str:
    """The accepted range in words. `[1..]` is precise and unreadable;
    the operator is being told what the form will refuse."""
    low, high = schema.min, schema.max
    if low is None and high is None:
        return ""
    if high is None:
        return f"{esc(_bound(low))} or more"  # type: ignore[arg-type]
    if low is None:
        return f"up to {esc(_bound(high))}"
    return f"{esc(_bound(low))} to {esc(_bound(high))}"


def _setting_row(view: ops.SettingView) -> str:
    s, st = view.schema, view.state
    changed = st.source is not Source.DEFAULT
    bounds = _bounds(s)
    unset = (
        button_form(
            f"/admin/settings/{s.key}/unset",
            "Unset",
            kind=ButtonKind.DANGER,
            describes=s.key,
        )
        if st.has_row
        else ""
    )
    return (
        f'<tr id="s-{esc(s.key)}" class="{"changed" if changed else ""}">'
        f"<td class=key><code>{esc(s.key)}</code>"
        f"<p class=desc>{esc(s.description)}</p></td>"
        f"<td class=muted>{esc(s.type)}"
        + (f"<p class=desc>{bounds}</p>" if bounds else "")
        + "</td>"
        f"<td class=muted><span class=mono>{esc(view.default)}</span></td>"
        f'<td><span class="source-{esc(st.source.value)}">'
        f"{esc(SOURCE_LABEL.get(st.source, st.source.value))}</span></td>"
        f'<td><form class=inline method=post action="/admin/settings/{esc(s.key)}">'
        f'<input type=text name=value value="{esc(st.value)}" aria-label="{esc(s.key)}">'
        f'<button aria-label="Save {esc(s.key)}">Save</button></form> {unset}</td>'
        "</tr>"
    )


@router.get("/settings")
def settings_page(_: AdminDep, ok: Flash = None, error: Flash = None) -> Response:
    views = ops.list_settings()
    groups: dict[str, list[ops.SettingView]] = {}
    for view in views:
        groups.setdefault(view.schema.group, []).append(view)
    changed = [v for v in views if v.state.source is not Source.DEFAULT]

    if changed:
        chips = "".join(
            f'<li><a href="#s-{esc(v.schema.key)}">{esc(v.schema.key)}</a></li>' for v in changed
        )
        summary = (
            "<div class=changed-summary>"
            f"<h3>{len(changed)} of {len(views)} settings differ from their defaults</h3>"
            f"<ul>{chips}</ul></div>"
        )
    else:
        summary = (
            "<div class=changed-summary><h3>Every setting is at its default</h3>"
            "<p>Nothing has been overridden in the database or the <code>.env</code> "
            "file.</p></div>"
        )

    jump = "".join(
        f'<a href="#g-{esc(name)}">{esc(name.replace("_", " ").capitalize())}</a>'
        for name in sorted(groups)
    )
    sections = []
    for name in sorted(groups):
        rows = groups[name]
        edited = sum(1 for v in rows if v.state.source is not Source.DEFAULT)
        count = f"{len(rows)} settings" + (f", {edited} changed" if edited else "")
        sections.append(
            f'<section class=group><h3 id="g-{esc(name)}">'
            f"{esc(name.replace('_', ' ').capitalize())} "
            f"<span class=count>{esc(count)}</span></h3>"
            "<div class=scroll><table class=settings>"
            "<colgroup><col class=c-key><col class=c-type><col class=c-default>"
            "<col class=c-source><col class=c-value></colgroup>"
            "<thead><tr><th>key</th><th>type</th><th>default</th>"
            "<th>source</th><th>effective value</th></tr></thead>"
            f"<tbody>{''.join(_setting_row(v) for v in rows)}</tbody></table></div></section>"
        )

    body = (
        "<h2>Settings</h2>"
        "<p class=note>The pipeline's DB-managed settings, the same ones <code>yfin config</code> "
        "edits. Save writes a row in the <code>settings</code> table after validating the value "
        "against the model; Unset deletes the row so the value falls back to <code>.env</code>, "
        "then to the default.</p>"
        "<p class=note>Running processes read the table at start, and only at start: it is never "
        "re-read. A change here reaches the next <code>yfin sync</code> by itself, but the keys "
        "the API process reads need the API restarted before they take effect. "
        "<code>yf_stream_publish_enabled</code> and <code>yf_stream_publish_redis_url</code> are "
        "the ones to watch: they decide whether the terminal's live feed runs at all, and until "
        "the restart every open page is still told the feed is off.</p>"
        f"{summary}"
        f"<div class=jump>{jump}</div>"
        f"{''.join(sections)}"
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
                button_form(
                    f"/admin/proxies/{p.id}/{ProxyAction.DISABLE}", "Disable", describes=p.label
                )
                if p.is_enabled
                else button_form(
                    f"/admin/proxies/{p.id}/{ProxyAction.ENABLE}", "Enable", describes=p.label
                ),
                button_form(
                    f"/admin/proxies/{p.id}/{ProxyAction.RESET}", "Reset health", describes=p.label
                ),
                button_form(
                    f"/admin/proxies/{p.id}/{ProxyAction.REMOVE}",
                    "Remove",
                    kind=ButtonKind.DANGER,
                    confirm=True,
                    describes=p.label,
                ),
            ]
        )
        rows.append(
            "<tr>"
            f"<td>{esc(p.label)}</td>"
            f"<td class=mono>{esc(p.scheme.value)}://{esc(p.host)}:{esc(p.port)}</td>"
            f"<td class=muted>{esc(p.username or '')}{' · secret' if p.password_enc else ''}</td>"
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
    table = (
        "<div class=scroll><table><thead><tr><th>label</th><th>endpoint</th><th>user</th>"
        "<th>enabled</th><th>health</th><th>ok</th><th>fail</th><th>consec.</th><th>ms</th>"
        "<th>cooldown until</th><th>last error</th><th></th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
        if rows
        else empty(
            "No proxies yet.",
            "Add one above and the next sync routes through it. Without a proxy the pipeline "
            "runs on this host's own address.",
        )
    )
    body = (
        "<h2>Proxies</h2>"
        "<p class=note>The pool <code>yfin sync</code> draws from. Health is written by the "
        "pipeline; Enable and Disable are the operator's decision and do not touch it. Reset "
        "health clears health, cooldown and the consecutive-failure counter, which brings a dead "
        "proxy back; the cumulative counters stay. Remove deletes the row.</p>"
        '<form method=post action="/admin/proxies" class=add-proxy>'
        "<div class=field><label for=proxy-url>Proxy URL</label>"
        '<input id=proxy-url type=text name=url placeholder="http://user:secret@host:port" '
        "size=42 required></div>"
        "<div class=field><label for=proxy-label>Label</label>"
        '<input id=proxy-label type=text name=label placeholder="optional"></div>'
        "<button class=primary>Add proxy</button></form>"
        "<p class=note>The URL form is the one <code>yfin proxy add</code> takes. The secret is "
        "stored Fernet-encrypted under <code>YF_PROXY_SECRET_KEY</code> and never shown "
        "again.</p>"
        f"{table}"
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
            button_form(
                f"/admin/screens/{s.screen_key}/{ScreenAction.DISABLE}",
                "Disable",
                describes=s.screen_key,
            )
            if s.is_enabled
            else button_form(
                f"/admin/screens/{s.screen_key}/{ScreenAction.ENABLE}",
                "Enable",
                describes=s.screen_key,
            )
        )
        rows.append(
            "<tr>"
            f"<td><code>{esc(s.screen_key)}</code>"
            f"<p class=desc>{esc(s.description)}</p></td>"
            f"<td>{esc(s.title)}</td>"
            f"<td class=muted>{esc(s.kind.value)} · {esc(s.quote_type.value)}</td>"
            f"<td>{yes_no(s.is_enabled)}</td><td>{toggle}</td>"
            "</tr>"
        )
    table = (
        "<div class=scroll><table><thead><tr><th>key</th><th>title</th><th>kind</th>"
        "<th>enabled</th><th></th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
        if rows
        else empty(
            "No screens seeded yet.",
            "They arrive with the first screener sync; until then there is nothing to switch.",
        )
    )
    body = (
        "<h2>Screens</h2>"
        "<p class=note>Which Yahoo screens <code>yfin sync --datasets screener</code> runs. "
        "The definition lives in <code>screens.py</code>; this switch is the runtime decision "
        "and the file does not turn a disabled screen back on.</p>"
        f"{table}"
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
    table = (
        "<div class=scroll><table><thead><tr><th>client</th><th>name</th><th>plan</th>"
        "<th>active</th><th>epoch</th><th>last used</th><th>created</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
        if rows
        else empty(
            "No API clients.",
            "Create the first one with yfin api client create; it will appear here.",
        )
    )
    body = (
        "<h2>API clients</h2>"
        "<p class=note>Read-only. Creating, rotating, revoking, scoping and disabling a client "
        "publishes a revocation to Redis so running workers drop its tokens; that path lives in "
        "<code>yfin api client</code> and is not duplicated here. Owner contact details are "
        "deliberately left out.</p>"
        f"{table}"
    )
    return page("API clients", "clients", body)
