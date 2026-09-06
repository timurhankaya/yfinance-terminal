"""Reads, validates, and writes the `settings` table.

This module is the only write gate for the configuration layer. Validation is
not embedded in the CLI command because a future admin panel would bypass
that command and hit raw SQL directly.
`yfin config set` is a thin wrapper around this module; the panel calls the
same functions.

Validation reuses the exact code path READ uses: a candidate value is tested
by building `Settings(**{**current_overrides, key: value})`. Two separate
validation paths would drift, and the panel could then accept a value that
the next run rejects.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import sqlalchemy
from pydantic import ValidationError
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from yfin.config import (
    DB_MANAGED_FIELDS,
    ENV_ONLY_FIELDS,
    Settings,
    bootstrap_settings,
    settings_from_overrides,
)
from yfin.logging_setup import get_logger
from yfin.models.settings import SettingRow

log = get_logger(__name__)

TABLE_NAME = "settings"
SEED_PATH = Path("config/settings.seed.json")


class SettingRejected(ValueError):
    """The value was not written. The CLI turns this into exit code 2.

    Exit code 2 means "configuration rejected" in this codebase (same
    pattern as `PruneDisabledError`); keeping it distinct from 1 lets a CI
    step tell "invalid setting" apart from "command errored".
    """


class Source(StrEnum):
    """Where the effective value came from."""

    DB = "db"
    ENV = "env"
    DEFAULT = "default"


@dataclass(frozen=True)
class SettingState:
    """The effective state of a single key.

    `has_row` is not the same as `source is Source.DB`: a key can have a row
    that was filtered out by `ENV_ONLY_FIELDS` and never applied. Collapsing
    both into one flag would hide that case.
    """

    key: str
    value: str
    source: Source
    has_row: bool


# --- serialization ----------------------------------------------------------


def serialize(value: Any) -> str:
    """Python value -> text stored in the `value` column.

    All DB-managed fields are scalar (bool / float / int / str). There is no
    rule for a complex type because none exists; the scalar test
    (`tests/unit/test_settings_split.py`) will fail if one is ever added,
    forcing this function to be updated.
    """
    if isinstance(value, bool):
        # Not "True": pydantic would still parse it, but it would break
        # round-trip equality with the `.env` format (`seed(export(state)) == state`).
        return "true" if value else "false"
    return str(value)


def normalize_key(raw: str) -> str:
    """Convert operator input into the canonical key.

    The canonical form is the model field name (lowercase). Operators will
    type `.env`-style `YF_MAX_SHARDS` out of habit; rejecting that would be
    needless friction. This doesn't conflict with the table's
    collation-sensitivity: the goal isn't to prevent collisions, it's to
    make a row inserted via raw SQL as `YF_MAX_SHARDS` show up as a distinct,
    visible "unknown key" warning instead of silently merging.
    """
    return raw.strip().lower()


# --- reading ------------------------------------------------------------


def _engine(settings: Settings) -> Engine:
    """Deliberately not `create_db_engine`.

    Three reasons: (1) called with no arguments it would recurse via
    `settings or get_settings()`, since the loader is already inside
    `get_settings()`; (2) pool sizing and `pool_pre_ping` are dead weight for
    a single SELECT; (3) without `connect_timeout` the CLI hangs for tens of
    seconds when the DB is unreachable. The repo already uses this pattern
    (`migrations/env.py` -> `poolclass=pool.NullPool`).
    """
    return create_engine(
        settings.db_url(),
        poolclass=NullPool,
        connect_args={"connect_timeout": 5},
    )


def fetch_rows(settings: Settings) -> dict[str, str] | None:
    """Raw rows from the table; `None` if the table doesn't exist.

    Table existence is checked via `inspect(engine).has_table()`, not by
    error code (MySQL 1146 / PG 42P01): code-based detection is
    engine-specific and would silently break on an engine change.

    Permission errors and an unreachable database propagate. Silently
    falling back to env-only would mean running with the wrong configuration.
    """
    engine = _engine(settings)
    try:
        if not sqlalchemy.inspect(engine).has_table(TABLE_NAME):
            # `yfin db upgrade` itself calls get_settings() before the table
            # exists; this is a normal setup state, not an error.
            log.info("settings tablosu yok; yalniz env kullaniliyor")
            return None
        with Session(engine) as session:
            rows = session.execute(select(SettingRow.setting_key, SettingRow.value)).all()
        return {key: value for key, value in rows}
    finally:
        engine.dispose()


class KeyVerdict(StrEnum):
    """Whether a key has a place in the DB layer."""

    OK = "ok"
    ENV_ONLY = "env_only"
    UNKNOWN = "unknown"


def classify_key(key: str) -> KeyVerdict:
    """The single source of truth for key policy.

    The read path (`filter_overrides`) and the write path (`validate_pair`)
    must make the same decision. When the decision was coded in both places
    -- as it originally was -- a later third category (e.g. deprecated keys)
    could be added to one and forgotten in the other. Forgetting it on the
    write path would just be annoying; forgetting it on the READ path would
    breach a security boundary: a DB row could override `db_host` and
    redirect the connection, or overwrite `yf_proxy_secret_key`.
    """
    if key in ENV_ONLY_FIELDS:
        return KeyVerdict.ENV_ONLY
    if key not in DB_MANAGED_FIELDS:
        return KeyVerdict.UNKNOWN
    return KeyVerdict.OK


# Decision is shared, messages are not: they aren't equally severe. An
# env-only row is a security event ("rejected"); an unknown key is usually
# a non-canonical name inserted via raw SQL ("ignored").
_LOG_MESSAGE = {
    KeyVerdict.ENV_ONLY: "settings satiri REDDEDILDI: env-only alan DB'den ezilemez",
    KeyVerdict.UNKNOWN: "settings satiri yok sayildi: bilinmeyen anahtar",
}
_REJECT_MESSAGE = {
    KeyVerdict.ENV_ONLY: (
        "{key} env-only bir alandir ve DB'den yonetilemez "
        "(baglanti bilgileri, Fernet anahtari, log seviyesi)."
    ),
    KeyVerdict.UNKNOWN: "bilinmeyen ayar anahtari: {key}",
}


def filter_overrides(rows: Mapping[str, str]) -> dict[str, str]:
    """Applicable rows. Nothing dropped passes silently.

    Invalid VALUES are not caught here: this function returns raw text, and
    the error surfaces as a `ValidationError` from `Settings(**overrides)`.
    That keeps value validation in one place, while key policy stays in
    `classify_key`.
    """
    out: dict[str, str] = {}
    for key, value in rows.items():
        verdict = classify_key(key)
        if verdict is KeyVerdict.OK:
            out[key] = value
        else:
            # `Settings` has extra="ignore" and silently swallows an unknown
            # kwarg (verified live); this warning is mandatory, not a nicety
            # -- skip it and no test goes red.
            log.warning(_LOG_MESSAGE[verdict], setting_key=key)
    return out


def load_overrides(settings: Settings) -> dict[str, str]:
    """DB overrides to apply (raw text).

    Bootstrap `Settings` is a parameter, not a test convenience but part of
    the contract: repo tests can point it at the test schema without
    calling `get_settings()`, which would otherwise recurse into its own
    loader.
    """
    rows = fetch_rows(settings)
    if rows is None:
        return {}
    return filter_overrides(rows)


def settings_state(*, rows: Mapping[str, str] | None = None) -> dict[str, SettingState]:
    """Effective value and source for every DB-managed key.

    Deliberately has no `Settings` parameter. It had one originally, unused,
    and the signature lied: callers (especially repo tests) assumed passing
    it steered the read, when the read comes entirely from `rows`. Whoever
    opens the connection calls `fetch_rows`; this function is pure.

    `rows=None` means the DB was not consulted (unreachable, or
    `YF_SETTINGS_SOURCE=env`); source then falls back to env/default and
    `yfin config list` doesn't crash. A recovery command must not fall
    victim to the outage it's meant to help recover from.

    Source is determined via `model_fields_set`, not by comparing "value
    differs from default": a setup where `.env` happens to match the
    default would otherwise show as `default`.
    """
    overrides = filter_overrides(rows) if rows is not None else {}
    env_only = bootstrap_settings()
    resolved = settings_from_overrides(overrides) if overrides else env_only

    out: dict[str, SettingState] = {}
    for key in sorted(DB_MANAGED_FIELDS):
        if key in overrides:
            source = Source.DB
        elif key in env_only.model_fields_set:
            source = Source.ENV
        else:
            source = Source.DEFAULT
        out[key] = SettingState(
            key=key,
            value=serialize(getattr(resolved, key)),
            source=source,
            has_row=rows is not None and key in rows,
        )
    return out


def export_values(
    states: Mapping[str, SettingState], *, all_keys: bool = False
) -> dict[str, Any]:
    """JSON body of `yfin config export`. Pure function.

    Values are returned as native types (int / bool / float / str), not
    text: the round-trip guarantee `seed(export(state)) == state` depends on
    it, and the seed file also uses native types.

    This used to be inline in the CLI; a repo test had to duplicate the same
    three lines, so the copy ended up testing itself rather than the actual
    output. Extracting it made this the single source of truth and testable
    without a DB.
    """
    resolved = settings_from_overrides({key: state.value for key, state in states.items()})
    return {
        key: getattr(resolved, key)
        for key, state in states.items()
        if all_keys or state.has_row
    }


# --- validation ---------------------------------------------------------


def validate_pair(key: str, value: str, *, overrides: Mapping[str, str]) -> None:
    """Reject a single (key, value) pair, or pass silently.

    Other overrides are included in the build because validation must reuse
    the read path's code; a future cross-field validator would then be
    caught here too.
    """
    verdict = classify_key(key)
    if verdict is not KeyVerdict.OK:
        raise SettingRejected(_REJECT_MESSAGE[verdict].format(key=key))
    candidate = {**overrides, key: value}
    try:
        settings_from_overrides(candidate)
    except ValidationError as exc:
        raise SettingRejected(f"{key} icin gecersiz deger {value!r}: {_first_error(exc)}") from exc


def _first_error(exc: ValidationError) -> str:
    errors = exc.errors()
    return str(errors[0].get("msg", exc)) if errors else str(exc)


# --- writing --------------------------------------------------------------


def _upsert(session: Session, key: str, value: str) -> None:
    """Update the existing row, or insert one.

    No engine-specific upsert clause (`ON CONFLICT` / `ON DUPLICATE KEY`):
    this is a ten-row table touched by one operator (or the panel), so
    engine neutrality is cheap to keep.
    """
    row = session.get(SettingRow, key)
    if row is None:
        session.add(SettingRow(setting_key=key, value=value))
    else:
        row.value = value


def set_setting(key: str, value: str, *, settings: Settings) -> str:
    """Validate and write. Returns the canonical key.

    An invalid value is rejected at write time; the error must not surface
    in the next night's cron run instead.
    """
    canonical = normalize_key(key)
    validate_pair(canonical, value, overrides=load_overrides(settings))
    engine = _engine(settings)
    try:
        with Session(engine) as session:
            _upsert(session, canonical, value)
            session.commit()
    finally:
        engine.dispose()
    return canonical


def unset_setting(key: str, *, settings: Settings) -> bool:
    """Delete the row. `False` if it didn't exist -- not an error (idempotent).

    A removed key falls back to `.env`, and from there to the model default.
    """
    canonical = normalize_key(key)
    engine = _engine(settings)
    try:
        with Session(engine) as session:
            row = session.get(SettingRow, canonical)
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True
    finally:
        engine.dispose()


def write_all(plan: Mapping[str, str], *, settings: Settings) -> None:
    """Write the plan in a single transaction (all or nothing)."""
    if not plan:
        return
    engine = _engine(settings)
    try:
        with Session(engine) as session:
            for key, value in plan.items():
                _upsert(session, key, value)
            session.commit()
    finally:
        engine.dispose()


# --- seeding ---------------------------------------------------------------


def load_seed_file(path: Path = SEED_PATH) -> dict[str, Any]:
    """`config/settings.seed.json` -- this installation's configuration.

    The file must not be a complete list: only keys that deviate from the
    default belong here. A key left out inherits the model default and
    tracks it when that default changes later -- a complete list would break
    that link.
    """
    if not path.exists():
        raise SettingRejected(f"tohum dosyasi bulunamadi: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SettingRejected(f"tohum dosyasi bir JSON nesnesi olmali: {path}")
    return data


def plan_seed(
    seed: Mapping[str, Any],
    existing: Mapping[str, str],
    *,
    force: bool = False,
    adopt_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Rows to write. Pure function: never touches the DB.

    Scope is limited to keys present in the JSON:

        key in JSON, no existing row -> JSON value is written
        key in JSON, row exists      -> untouched (overwritten with --force)
        key not in JSON              -> nothing happens (--force included)

    That last rule matters. An earlier draft defined seeding as "fill every
    missing row", which meant `seed` silently undid a prior `unset` -- the
    two commands would fight each other. `--force` also only overwrites keys
    present in the JSON; otherwise it would silently erase every override an
    operator made from the panel.

    All-or-nothing: the whole JSON is validated first, then written in one
    transaction. A partially written seed would leave it unclear which key
    came from which source.
    """
    plan: dict[str, str] = {}
    validated: dict[str, str] = {}
    for raw_key, raw_value in seed.items():
        key = normalize_key(str(raw_key))
        value = serialize(raw_value)
        validate_pair(key, value, overrides=validated)
        validated[key] = value

    for key, value in validated.items():
        if force or key not in existing:
            plan[key] = value

    if adopt_env is not None:
        # One-time, for migration. A setup with YF_MAX_SHARDS=8 in `.env`
        # would otherwise silently drop back to 4 after this migration.
        for key, value in adopt_env.items():
            if key in validated or key in existing:
                continue
            plan[key] = value
    return plan


def adopt_env_values() -> dict[str, str]:
    """Effective values read from a bootstrap with the DB layer disabled.

    Does not use `get_settings()`: doing so would feed seeding its own
    freshly written rows back to itself, turning `--adopt-env` from
    "adopt the env" into "copy the DB from the DB".
    """
    env_only = bootstrap_settings()
    return {key: serialize(getattr(env_only, key)) for key in sorted(DB_MANAGED_FIELDS)}
