"""Database operations behind the admin page, as plain functions.

Settings go through `yfin.storage.settings_store`, the same validated path
`yfin config set` takes. Nothing here renders HTML.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from yfin.api.models.clients import ApiClient
from yfin.core.config import FieldSchema, bootstrap_settings, settings_schema
from yfin.models import Proxy, ProxyHealth, Screen
from yfin.storage.settings_store import (
    SettingRejected,
    SettingState,
    fetch_rows,
    serialize,
    set_setting,
    settings_state,
    unset_setting,
)


class AdminError(ValueError):
    """A refusal phrased for the operator; the page shows it verbatim."""


# --- settings ---------------------------------------------------------------


@dataclass(frozen=True)
class SettingView:
    schema: FieldSchema
    state: SettingState
    default: str


def list_settings() -> list[SettingView]:
    """Every DB-managed setting with its schema and effective state.
    Unreachable database: the state falls back to env/default, exactly
    as `yfin config list` does."""
    settings = bootstrap_settings()
    try:
        rows = fetch_rows(settings)
    except Exception:  # noqa: BLE001 - the page must render during an outage
        rows = None
    states = settings_state(rows=rows)
    return [
        SettingView(schema=item, state=states[item.key], default=serialize(item.default))
        for item in settings_schema()
    ]


def save_setting(key: str, value: str) -> str:
    try:
        return set_setting(key, value, settings=bootstrap_settings())
    except SettingRejected as exc:
        raise AdminError(str(exc)) from exc


def clear_setting(key: str) -> bool:
    return unset_setting(key, settings=bootstrap_settings())


# --- proxies ----------------------------------------------------------------


def list_proxies(session: Session) -> list[Proxy]:
    return list(session.execute(select(Proxy).order_by(Proxy.label)).scalars())


def add_proxy(session: Session, url: str, label: str | None) -> Proxy:
    """`scheme://[user:secret@]host:port`, the same DSN `yfin proxy add`
    takes; the secret is stored Fernet-encrypted, never in clear."""
    from yfin import proxy as px

    try:
        endpoint = px.parse_dsn(url)
    except ValueError as exc:
        raise AdminError(str(exc)) from exc
    try:
        password_enc = px.encrypt_password(endpoint.password, bootstrap_settings())
    except px.SecretKeyMissing as exc:
        raise AdminError(str(exc)) from exc
    existing = session.execute(
        select(Proxy).where(
            Proxy.scheme == endpoint.scheme,
            Proxy.host == endpoint.host,
            Proxy.port == endpoint.port,
            Proxy.username == endpoint.username,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise AdminError(f"already present as {existing.label}")
    row = Proxy(
        label=label or px.default_label(endpoint),
        scheme=endpoint.scheme,
        host=endpoint.host,
        port=endpoint.port,
        username=endpoint.username,
        password_enc=password_enc,
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise AdminError(f"that label is already in use: {row.label}") from exc
    return row


class ProxyAction(enum.StrEnum):
    """What the proxies page can do to one row; the URL's last segment."""

    ENABLE = "enable"
    DISABLE = "disable"
    RESET = "reset"
    REMOVE = "remove"


class ScreenAction(enum.StrEnum):
    ENABLE = "enable"
    DISABLE = "disable"


def proxy_action(session: Session, proxy_id: int, action: ProxyAction) -> str:
    """Returns the label acted on. `reset` keeps the cumulative counters,
    like `yfin proxy reset`: past data is not deleted."""
    row = session.get(Proxy, proxy_id)
    if row is None:
        raise AdminError(f"no proxy with id {proxy_id}")
    if action in (ProxyAction.ENABLE, ProxyAction.DISABLE):
        session.execute(
            update(Proxy).where(Proxy.id == row.id).values(is_enabled=action is ProxyAction.ENABLE)
        )
    elif action is ProxyAction.RESET:
        session.execute(
            update(Proxy)
            .where(Proxy.id == row.id)
            .values(
                health=ProxyHealth.UNKNOWN,
                consecutive_failures=0,
                cooldown_rounds=0,
                cooldown_until=None,
                last_error=None,
            )
        )
    elif action is ProxyAction.REMOVE:
        session.execute(delete(Proxy).where(Proxy.id == row.id))
    else:  # pragma: no cover - the enum is exhaustive
        raise AdminError(f"unknown action {action}")
    label = row.label
    session.commit()
    return label


# --- screens ----------------------------------------------------------------


def list_screens(session: Session) -> list[Screen]:
    return list(session.execute(select(Screen).order_by(Screen.screen_key)).scalars())


def set_screen_enabled(session: Session, screen_key: str, enabled: bool) -> None:
    row = session.get(Screen, screen_key)
    if row is None:
        raise AdminError(f"no screen named {screen_key}")
    session.execute(
        update(Screen).where(Screen.screen_key == screen_key).values(is_enabled=enabled)
    )
    session.commit()


# --- API clients (read-only here; `yfin api client` changes them) -----------


def list_clients(session: Session) -> list[ApiClient]:
    return list(session.execute(select(ApiClient).order_by(ApiClient.client_id)).scalars())
