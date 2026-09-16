"""`yfin config` command group: THIN wrappers over `settings_store`.

The admin panel calls the same store functions, so logic buried here would be
logic the panel bypasses. `list`/`get`/`schema` don't crash when the DB is
unreachable: a recovery command must not be a casualty of what it recovers from."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Annotated

import typer

from yfin.core.config import (
    DB_MANAGED_FIELDS,
    SETTING_GROUPS,
    Settings,
    bootstrap_settings,
    settings_schema,
    source_is_env,
)

if TYPE_CHECKING:
    from yfin.storage.settings_store import SettingState

config_app = typer.Typer(help="DB-backed configuration (settings table)", no_args_is_help=True)

# Configuration REJECTION: distinct from a plain command error (1) so a CI step
# can tell "invalid setting" apart from "command crashed".
EXIT_REJECTED = 2


def _read_rows_or_warn(settings: Settings) -> dict[str, str] | None:
    """Read rows; `None` if the DB layer is off or unreachable.

    `None` means "DB WAS NOT CONSULTED", and `settings_state` reflects that
    correctly by reporting the effective source as env/default.
    """
    from yfin.storage.settings_store import fetch_rows

    if source_is_env():
        typer.echo(
            "DB layer OFF (YF_SETTINGS_SOURCE=env): the values below come "
            "from .env and the model defaults. "
            "`set` / `unset` / `seed` still WRITE to the database.",
            err=True,
        )
        return None
    try:
        return fetch_rows(settings)
    except Exception as exc:  # noqa: BLE001 - a recovery command must not crash
        typer.echo(
            f"could not read the settings table ({exc}); continuing with env/default.",
            err=True,
        )
        return None


def _states(settings: Settings) -> dict[str, SettingState]:
    from yfin.storage.settings_store import settings_state

    return settings_state(rows=_read_rows_or_warn(settings))


def _defaults() -> dict[str, str]:
    from yfin.storage.settings_store import serialize

    return {item.key: serialize(item.default) for item in settings_schema()}


@config_app.command("list")
def config_list(
    group: Annotated[str | None, typer.Option("--group", help="Only this group")] = None,
    changed: Annotated[
        bool, typer.Option("--changed", help="Only values DIFFERENT from the model default")
    ] = False,
    source: Annotated[
        str | None, typer.Option("--source", help="db | env | default")
    ] = None,
) -> None:
    """Effective value and source for each DB-managed setting.

    Env-only fields are not listed: they can't be managed from the panel.
    `--changed` asks "is the effective value different from the default", not
    "does a row exist"; use `--source db` for row existence."""
    from yfin.storage.settings_store import Source

    if group is not None and group not in SETTING_GROUPS:
        typer.echo(f"unknown group: {group} ({', '.join(SETTING_GROUPS)})", err=True)
        raise typer.Exit(code=EXIT_REJECTED)
    if source is not None and source not in {s.value for s in Source}:
        typer.echo(f"unknown source: {source} (db | env | default)", err=True)
        raise typer.Exit(code=EXIT_REJECTED)

    settings = bootstrap_settings()
    states = _states(settings)
    groups = {item.key: item.group for item in settings_schema()}
    defaults = _defaults()

    shown = 0
    for key, state in states.items():
        if group is not None and groups[key] != group:
            continue
        if source is not None and state.source.value != source:
            continue
        if changed and state.value == defaults[key]:
            continue
        # `*` = no row. Must be marked: after migration the `.env` layer is
        # effectively empty, so a keyless entry falls straight to the model
        # default.
        mark = " " if state.has_row else "*"
        typer.echo(f"{mark} {key:<34} {state.value:<28} {state.source.value:<8} {groups[key]}")
        shown += 1
    typer.echo(f"-- {shown} settings ('*' = no settings row)")


@config_app.command("get")
def config_get(key: Annotated[str, typer.Argument(help="Setting key")]) -> None:
    """Effective value and source of a single setting."""
    from yfin.storage.settings_store import normalize_key

    canonical = normalize_key(key)
    settings = bootstrap_settings()
    states = _states(settings)
    state = states.get(canonical)
    if state is None:
        typer.echo(f"unknown or not DB-managed setting: {canonical}", err=True)
        raise typer.Exit(code=EXIT_REJECTED)
    mark = "" if state.has_row else "  (no settings row)"
    typer.echo(f"{state.key} = {state.value}  [{state.source.value}]{mark}")


@config_app.command("set")
def config_set(
    key: Annotated[str, typer.Argument(help="Setting key")],
    value: Annotated[str, typer.Argument(help="Value (text; pydantic resolves the type)")],
) -> None:
    """Validate and write a `settings` row.

    An invalid value is rejected AT WRITE TIME (exit 2); the error must not
    surface in tomorrow night's cron run instead.
    """
    from yfin.storage.settings_store import SettingRejected, set_setting

    try:
        canonical = set_setting(key, value, settings=bootstrap_settings())
    except SettingRejected as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=EXIT_REJECTED) from None
    typer.echo(f"{canonical} = {value}")


@config_app.command("unset")
def config_unset(key: Annotated[str, typer.Argument(help="Setting key")]) -> None:
    """Delete the row; the key falls back to `.env` and the model default.

    Exit code 0 and a message if no row existed: the command is IDEMPOTENT.
    """
    from yfin.storage.settings_store import (
        SEED_PATH,
        SettingRejected,
        load_seed_file,
        normalize_key,
        serialize,
        unset_setting,
    )

    canonical = normalize_key(key)
    if canonical not in DB_MANAGED_FIELDS:
        typer.echo(f"unknown or not DB-managed setting: {canonical}", err=True)
        raise typer.Exit(code=EXIT_REJECTED)

    settings = bootstrap_settings()
    removed = unset_setting(canonical, settings=settings)
    typer.echo(f"{canonical}: row deleted" if removed else f"{canonical}: there was no row")

    # `settings` here is a bootstrap instance with the DB layer disabled, so
    # it already carries the result of the `.env` -> default chain: exactly
    # the value that becomes effective once the row is gone.
    typer.echo(f"value now in effect: {serialize(getattr(settings, canonical))}")

    # If the key is in the seed file, `unset` is NOT PERMANENT and this must
    # be said: the JSON is "this deployment's configuration", the next
    # `seed` run puts the value back.
    try:
        seed = {normalize_key(str(k)) for k in load_seed_file(SEED_PATH)}
    except (SettingRejected, ValueError):
        seed = set()
    if canonical in seed:
        typer.echo(
            f"WARNING: {canonical} is defined in {SEED_PATH}; "
            "the next `yfin config seed` will put it back."
        )


@config_app.command("seed")
def config_seed(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Only print the plan")] = False,
    force: Annotated[
        bool, typer.Option("--force", help="OVERWRITE existing rows for keys present in the JSON")
    ] = False,
    adopt_env: Annotated[
        bool,
        typer.Option("--adopt-env", help="Use the effective .env value for rowless, non-JSON keys"),
    ] = False,
) -> None:
    """Apply `config/settings.seed.json`.

    Scope is ONLY the keys in the JSON; even `--force` never touches a row
    outside it, which would erase operator overrides. `--dry-run` exits 1 if
    any row is missing, so CI can use it as an "is the seed up to date" step."""
    from yfin.storage.settings_store import (
        SEED_PATH,
        SettingRejected,
        adopt_env_values,
        fetch_rows,
        load_seed_file,
        plan_seed,
        write_all,
    )

    settings = bootstrap_settings()
    try:
        seed = load_seed_file(SEED_PATH)
    except (SettingRejected, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=EXIT_REJECTED) from None

    try:
        rows = fetch_rows(settings)
    except Exception as exc:  # noqa: BLE001 - if it can't be read, don't try to write either
        typer.echo(f"settings tablosu okunamadi: {exc}", err=True)
        raise typer.Exit(code=1) from None
    if rows is None:
        typer.echo("no settings table; run `yfin db upgrade` first.", err=True)
        raise typer.Exit(code=1)

    try:
        plan = plan_seed(
            seed,
            rows,
            force=force,
            adopt_env=adopt_env_values() if adopt_env else None,
        )
    except SettingRejected as exc:
        # ALL OR NOTHING: an invalid JSON writes NOTHING.
        typer.echo(f"seed rejected, nothing was written: {exc}", err=True)
        raise typer.Exit(code=EXIT_REJECTED) from None

    for key, value in sorted(plan.items()):
        typer.echo(f"{'[dry-run] ' if dry_run else ''}{key} = {value}")

    if dry_run:
        typer.echo(f"-- {len(plan)} rows would be written")
        raise typer.Exit(code=1 if plan else 0)

    write_all(plan, settings=settings)
    typer.echo(f"-- {len(plan)} rows written")


@config_app.command("export")
def config_export(
    all_keys: Annotated[
        bool, typer.Option("--all", help="Full snapshot of every DB-managed value")
    ] = False,
) -> None:
    """Always prints JSON.

    By default includes only keys WITH A DB ROW, the inverse of the seed file.
    `--all` adds model defaults; that output must NOT be written to
    `config/settings.seed.json`, or a future default change would not reach here."""
    from yfin.storage.settings_store import export_values

    states = _states(bootstrap_settings())
    typer.echo(
        json.dumps(
            export_values(states, all_keys=all_keys), indent=2, sort_keys=True, ensure_ascii=False
        )
    )


@config_app.command("schema")
def config_schema(
    as_json: Annotated[bool, typer.Option("--json", help="Panel-ready format")] = False,
) -> None:
    """Metadata the panel needs to draw its form. Never touches the DB."""
    from yfin.storage.settings_store import serialize

    note = (
        "Note: the env-only fields (db_*, yf_proxy_secret_key, log_level, "
        "log_format, metrics_port, yf_stream_publish_redis_url) "
        "are NOT listed here; they stay in .env."
    )
    items = settings_schema()
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "note": note,
                    "fields": [
                        {
                            "key": i.key,
                            "group": i.group,
                            "type": i.type,
                            "default": i.default,
                            "min": i.min,
                            "max": i.max,
                            "description": i.description,
                        }
                        for i in items
                    ],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    typer.echo(note)
    for item in items:
        bounds = ""
        if item.min is not None or item.max is not None:
            bounds = f" [{item.min if item.min is not None else '-'}"
            bounds += f"..{item.max if item.max is not None else '-'}]"
        typer.echo(
            f"{item.key:<34} {item.type:<6} {serialize(item.default):<28} "
            f"{item.group:<12}{bounds} {item.description}"
        )
