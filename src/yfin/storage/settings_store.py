"""Reads, validates, and writes the `settings` table; the only write gate.

Validation reuses the read path: a candidate is tested by building
`Settings(**{**current_overrides, key: value})`, so the two cannot drift.
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

from yfin.core.config import (
    DB_MANAGED_FIELDS,
    ENV_ONLY_FIELDS,
    Settings,
    bootstrap_settings,
    settings_from_overrides,
)
from yfin.core.logging_setup import get_logger
from yfin.models.settings import SettingRow

log = get_logger(__name__)

TABLE_NAME = "settings"
SEED_PATH = Path("config/settings.seed.json")


class SettingRejected(ValueError):
    """The value was not written. The CLI turns this into exit code 2 ("rejected")."""


class Source(StrEnum):
    """Where the effective value came from."""

    DB = "db"
    ENV = "env"
    DEFAULT = "default"


@dataclass(frozen=True)
class SettingState:
    """The effective state of a single key.

    `has_row` differs from `source is Source.DB`: an env-only row exists but is never applied.
    """

    key: str
    value: str
    source: Source
    has_row: bool


# --- serialization ----------------------------------------------------------


def serialize(value: Any) -> str:
    """Python value -> text stored in the `value` column.

    All DB-managed fields are scalar; `test_settings_split.py` enforces that.
    """
    if isinstance(value, bool):
        # Not "True": pydantic would still parse it, but it would break
        # round-trip equality with the `.env` format (`seed(export(state)) == state`).
        return "true" if value else "false"
    return str(value)


def normalize_key(raw: str) -> str:
    """Convert operator input into the canonical key (the lowercase field name).

    A row inserted via raw SQL as `YF_MAX_SHARDS` stays a distinct,
    visible "unknown key" rather than silently merging.
    """
    return raw.strip().lower()


# --- reading ------------------------------------------------------------


def _engine(settings: Settings) -> Engine:
    """Not `create_db_engine`: that would recurse into `get_settings()`, and a
    single SELECT needs no pool but does need `connect_timeout`."""
    return create_engine(
        settings.db_url(),
        poolclass=NullPool,
        connect_args={"connect_timeout": 5},
    )


def fetch_rows(settings: Settings) -> dict[str, str] | None:
    """Raw rows from the table; `None` if the table doesn't exist.

    Permission errors and an unreachable database propagate: falling back
    to env-only would mean running with the wrong configuration.
    """
    engine = _engine(settings)
    try:
        if not sqlalchemy.inspect(engine).has_table(TABLE_NAME):
            # `yfin db upgrade` itself calls get_settings() before the table
            # exists; this is a normal setup state, not an error.
            log.info("no settings table; using env only")
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

    The read path and the write path must agree: a miss on the read path
    would let a DB row override `db_host` or `yf_proxy_secret_key`.
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
    KeyVerdict.ENV_ONLY: "settings row REJECTED: env-only field, not overridable from the DB",
    KeyVerdict.UNKNOWN: "settings row ignored: unknown key",
}
_REJECT_MESSAGE = {
    KeyVerdict.ENV_ONLY: (
        "{key} is an env-only field and cannot be managed from the database "
        "(connection details, Fernet key, log level)."
    ),
    KeyVerdict.UNKNOWN: "unknown setting key: {key}",
}


def filter_overrides(rows: Mapping[str, str]) -> dict[str, str]:
    """Applicable rows. Nothing dropped passes silently.

    Values are not validated here; `Settings(**overrides)` does that.
    """
    out: dict[str, str] = {}
    for key, value in rows.items():
        verdict = classify_key(key)
        if verdict is KeyVerdict.OK:
            out[key] = value
        else:
            # `Settings` has extra="ignore" and would swallow an unknown kwarg
            # silently; this warning is the only signal.
            log.warning(_LOG_MESSAGE[verdict], setting_key=key)
    return out


def load_overrides(settings: Settings) -> dict[str, str]:
    """DB overrides to apply (raw text).

    Takes bootstrap `Settings` so callers never go through `get_settings()`,
    which would recurse into this loader.
    """
    rows = fetch_rows(settings)
    if rows is None:
        return {}
    return filter_overrides(rows)


def settings_state(*, rows: Mapping[str, str] | None = None) -> dict[str, SettingState]:
    """Effective value and source for every DB-managed key. Pure function.

    `rows=None` means the DB was not consulted; source falls back to
    env/default. Source uses `model_fields_set`, so `.env` matching the default is `env`.
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

    Values are native types, not text: `seed(export(state)) == state` depends on it.
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

    Other overrides are included so a cross-field validator is exercised too.
    """
    verdict = classify_key(key)
    if verdict is not KeyVerdict.OK:
        raise SettingRejected(_REJECT_MESSAGE[verdict].format(key=key))
    candidate = {**overrides, key: value}
    try:
        settings_from_overrides(candidate)
    except ValidationError as exc:
        raise SettingRejected(f"invalid value {value!r} for {key}: {_first_error(exc)}") from exc


def _first_error(exc: ValidationError) -> str:
    errors = exc.errors()
    return str(errors[0].get("msg", exc)) if errors else str(exc)


# --- writing --------------------------------------------------------------


def _upsert(session: Session, key: str, value: str) -> None:
    """Update the existing row, or insert one. Engine-neutral on purpose."""
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

    Only keys that deviate from the default belong here; a key left out
    keeps tracking the model default.
    """
    if not path.exists():
        raise SettingRejected(f"seed file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SettingRejected(f"the seed file must be a JSON object: {path}")
    return data


def plan_seed(
    seed: Mapping[str, Any],
    existing: Mapping[str, str],
    *,
    force: bool = False,
    adopt_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Rows to write. Pure function: never touches the DB.

    Only keys present in the JSON are touched, even with `--force`, so
    `seed` never undoes an `unset` or a panel override. Validated as a whole first.
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

    Not `get_settings()`: that would feed freshly seeded rows back to seeding.
    """
    env_only = bootstrap_settings()
    return {key: serialize(getattr(env_only, key)) for key in sorted(DB_MANAGED_FIELDS)}
