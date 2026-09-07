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



@db_app.command("monitor-role")
def db_monitor_role(
    role: Annotated[str, typer.Option("--role", help="Role name")] = "yfin_monitor",
    password_env: Annotated[
        str,
        typer.Option("--password-env", help="Environment variable holding the password"),
    ] = "MONITOR_DB_PASSWORD",
) -> None:
    """Creates the read-only role the metrics exporters log in as.

    A COMMAND rather than a migration, for four reasons that each rule it
    out on their own: a role is cluster-wide while migrations run against a
    database, the repo tests run migrations in parallel per-process schemas,
    a password written into a migration lands in `log_statement`, and
    rotating one would need a new revision forever.

    Idempotent, so it can be re-run to rotate the password: `CREATE ROLE`
    has no `IF NOT EXISTS`, so existence is checked in a `DO $$` block and
    the password is set either way.

    `pg_monitor` and nothing else. It is enough for Alloy's default
    postgres collectors -- `pg_stat_*`, sizes, replication -- and it grants
    no `SELECT` on any data table. The exporter is meant to see how the
    database is doing, not what is in it; when a custom query eventually
    needs a table, that grant should be visible in a diff rather than
    already in place.
    """
    import os

    from sqlalchemy import text

    from yfin.cli.common import engine

    password = os.environ.get(password_env)
    if not password:
        typer.echo(f"{password_env} is not set; refusing to create a role without a password")
        raise typer.Exit(code=1)

    db = engine()
    with db.connect() as conn:
        # Role names and passwords cannot be bind parameters: CREATE ROLE and
        # ALTER ROLE are utility statements and PostgreSQL rejects a
        # placeholder in them. So POSTGRESQL does the quoting, through
        # `quote_ident` and `quote_literal` in ordinary SELECTs that do take
        # parameters, and only the already-quoted text is interpolated.
        # Quoting either by hand here would be an injection waiting for a
        # password with an apostrophe in it.
        #
        # The password does end up in the text of the statement that sets it,
        # and `log_statement = all` would record it. That is unavoidable for
        # ALTER ROLE, and it is one more reason this is an operator command
        # run once rather than a migration -- which would keep the password
        # in the repository and replay it on every deployment.
        ident = conn.execute(text("SELECT quote_ident(:r)"), {"r": role}).scalar_one()
        secret = conn.execute(
            text("SELECT quote_literal(:p)"), {"p": password}
        ).scalar_one()

        # `CREATE ROLE` has no `IF NOT EXISTS`, so the branch is here. Two
        # operators running this at the same second would have one of them
        # see "role already exists"; that is a legible failure for a command
        # a person runs by hand, and not worth a lock.
        exists = conn.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
        ).scalar_one_or_none()
        if not exists:
            conn.execute(text(f"CREATE ROLE {ident} LOGIN"))

        # Runs whether or not the role was just created, so re-running the
        # command rotates the password.
        conn.execute(text(f"ALTER ROLE {ident} WITH LOGIN PASSWORD {secret}"))
        conn.execute(text(f"GRANT pg_monitor TO {ident}"))
        conn.commit()
    typer.echo(f"role ready: {role} (pg_monitor, no data access)")
