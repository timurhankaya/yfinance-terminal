"""`yfin db` -- schema creation and migration.

Alembic and `Base.metadata` are imported inside the command bodies, not at
module level: `yfin --help` used to pull the whole model package in through
this group even though nothing but these three commands ever needs it.
"""

from __future__ import annotations

from typing import Annotated

import typer

from yfin.core.config import get_settings
from yfin.core.logging_setup import configure_logging, get_logger

log = get_logger(__name__)

db_app = typer.Typer(help="Database operations", no_args_is_help=True)


@db_app.command("upgrade")
def db_upgrade(
    revision: Annotated[str, typer.Argument(help="Target revision")] = "head",
) -> None:
    """Applies Alembic migrations."""
    from alembic import command
    from alembic.config import Config

    configure_logging(get_settings().log_level)
    cfg = Config("alembic.ini")
    command.upgrade(cfg, revision)
    typer.echo(f"migration uygulandi: {revision}")
    _warn_missing_settings_rows()


def _warn_missing_settings_rows() -> None:
    """Warns in one line about DB-managed keys that have no settings row.

    After migrating to the DB-backed layer, the `.env` layer is effectively
    empty; if `yfin config seed` is not run for a field newly added to
    `Settings`, its value falls through to the model default instead of
    `.env`. `seed` is therefore a standard step after every `upgrade`, and
    this command reminds the operator of that.

    This reads `Settings.model_fields` from a command, not a migration, so it
    does not violate the rule against migrations importing application code.
    """
    from yfin.core.config import DB_MANAGED_FIELDS, bootstrap_settings, source_is_env
    from yfin.storage.settings_store import fetch_rows

    if source_is_env():
        return
    try:
        rows = fetch_rows(bootstrap_settings())
    except Exception as exc:  # noqa: BLE001 - warning path, must not crash the command
        typer.echo(
            f"could not read the settings table, skipping the missing-row check: {exc}",
            err=True,
        )
        return
    if rows is None:
        return
    missing = sorted(DB_MANAGED_FIELDS - set(rows))
    if missing:
        typer.echo(
            f"{len(missing)} settings have no `settings` row (first: {missing[0]}); "
            "their values will come from .env or the model default. "
            "Run `yfin config seed`."
        )


@db_app.command("create")
def db_create() -> None:
    """Creates the database if it does not exist.

    Never touches the DB-backed configuration layer: the command that creates
    the database cannot connect to a database that does not exist yet, but
    `get_settings()` would try to connect to exactly that database to read
    the `settings` table.
    """
    from sqlalchemy import create_engine, text

    from yfin.core.config import bootstrap_settings

    settings = bootstrap_settings()
    # AUTOCOMMIT is required: PostgreSQL rejects `CREATE DATABASE` inside a
    # transaction block.
    engine = create_engine(settings.bootstrap_url(), isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        for name in (settings.db_name, settings.db_test_name):
            # PostgreSQL has no `CREATE DATABASE IF NOT EXISTS`; existence is
            # checked via pg_database instead.
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine.dispose()

    # The extension is installed separately per database: `CREATE EXTENSION`
    # is database-scoped. It does not go in a migration -- that would break
    # symmetry with `downgrade`, and the extension is a product of `db create`.
    for name in (settings.db_name, settings.db_test_name):
        db_engine = create_engine(settings.db_url(name), isolation_level="AUTOCOMMIT")
        with db_engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
        db_engine.dispose()

    typer.echo(f"veritabani hazir: {settings.db_name}, {settings.db_test_name}")


@db_app.command("revision")
def db_revision(message: Annotated[str, typer.Option("-m", "--message")]) -> None:
    """Generates a new migration from the models.

    Disables the DB-backed configuration layer. `migrations/env.py` calls
    `get_settings()`; a `revision` run before the schema exists would
    otherwise fail looking for the `settings` table. The env var has to be
    set directly, since the key that disables the layer cannot itself be
    read from the layer (chicken-and-egg).
    """
    import os

    from alembic import command
    from alembic.config import Config

    from yfin.core.config import SETTINGS_SOURCE_VAR

    os.environ[SETTINGS_SOURCE_VAR] = "env"
    command.revision(Config("alembic.ini"), message=message, autogenerate=True)

