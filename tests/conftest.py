"""Shared test fixtures."""

from __future__ import annotations

import os

# Module level, before any `yfin` import.
#
# An autouse session fixture is not enough: pytest imports all test
# modules first, fixtures run after -- a `Settings` loader set up at
# import time would already have fired. Also a session-scoped fixture
# can't depend on a function-scoped `monkeypatch` (ScopeMismatch).
#
# Required because `load_overrides` binds to `db_name`, i.e. the
# production schema, while tests run in a process-specific schema
# (`tests/helpers.py`). Uses `setdefault` so repo tests exercising the DB
# path can deliberately lift this guard via `monkeypatch.delenv` +
# `config.reset_settings()`.
#
# Spawned children inherit `os.environ` (shard.py), so the variable
# propagates to them too.
os.environ.setdefault("YF_SETTINGS_SOURCE", "env")

from collections.abc import Iterator  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import Engine, create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

import yfin.api.models  # noqa: E402,F401  API tables share Base.metadata
from helpers import drop_stale_schemas, schema_name  # noqa: E402
from yfin.core.config import Settings, get_settings  # noqa: E402
from yfin.models import (  # noqa: E402
    V_ACTIONS_CREATE,
    V_PRICE_BARS_REGULAR_CREATE,
    Base,
    all_timescale_ddl,
)
from yfin.storage.db import create_db_engine  # noqa: E402


@pytest.fixture(scope="session")
def settings() -> Settings:
    return get_settings()


@pytest.fixture(scope="session")
def test_schema(settings: Settings) -> str:
    """Schema name for this pytest process."""
    return schema_name(settings.db_test_name)


@pytest.fixture(scope="session")
def bootstrap_engine(settings: Settings) -> Iterator[Engine]:
    """`postgres` maintenance database, for `CREATE DATABASE` only.

    Schema operations can't use this engine: `information_schema` is
    database-specific, so a connection opened here can't see schemas
    inside `yfinance_test`.

    AUTOCOMMIT is required: `CREATE DATABASE` cannot run in a transaction
    block.
    """
    engine = create_engine(settings.bootstrap_url(), isolation_level="AUTOCOMMIT")
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def test_db_engine(settings: Settings, bootstrap_engine: Engine) -> Iterator[Engine]:
    """Engine bound to the test database (no schema selected).

    Used for schema create/drop and stale-schema cleanup. The extension
    is installed here because `CREATE EXTENSION` is database-scoped;
    skipping it makes `create_hypertable` fail with "function
    by_range(unknown, interval) does not exist".
    """
    try:
        with bootstrap_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"),
                {"n": settings.db_test_name},
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{settings.db_test_name}"'))
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"PostgreSQL unreachable: {exc}")

    engine = create_engine(
        settings.db_url(settings.db_test_name), isolation_level="AUTOCOMMIT"
    )
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def test_engine(
    settings: Settings, test_schema: str, test_db_engine: Engine
) -> Iterator[Engine]:
    """Process-specific test schema; fully dropped at the end of the run."""
    drop_stale_schemas(test_db_engine, settings.db_test_name)
    with test_db_engine.connect() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{test_schema}" CASCADE'))
        conn.execute(text(f'CREATE SCHEMA "{test_schema}"'))

    # `public` must stay on the search_path: the timescaledb extension
    # lives there, and create_hypertable / timescaledb_information.* can't
    # resolve otherwise. Omitting it makes tests silently fall back to a
    # plain table.
    engine = create_db_engine(
        settings,
        settings.db_test_name,
        schema=test_schema,
        application_name=f"yfin-pytest-{os.getpid()}",
    )
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        conn.execute(text(V_ACTIONS_CREATE))
        conn.execute(text(V_PRICE_BARS_REGULAR_CREATE))
        # create_all() doesn't know about hypertables (Alembic can't
        # autogenerate them either). Without applying the same DDL as the
        # migration, price_bars stays a plain table and chunk behavior
        # can never be verified.
        for stmt in all_timescale_ddl():
            conn.execute(text(stmt))
        conn.commit()
    yield engine
    engine.dispose()

    # `DROP SCHEMA ... CASCADE` also cleans up chunks (observed: "drop
    # cascades to table _timescaledb_internal._hyper_1_1_chunk").
    with test_db_engine.connect() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{test_schema}" CASCADE'))


@pytest.fixture
def db_session(test_engine: Engine) -> Iterator[Session]:
    """Each test runs in its own transaction, rolled back at the end."""
    connection = test_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def committed_session(test_engine: Engine) -> Iterator[Session]:
    """Session that actually commits, for concurrency tests.

    `db_session` opens one connection and rolls back at the end, so a
    second session never sees its writes and no deadlock can occur. This
    fixture cleans up its own rows.
    """
    session = Session(bind=test_engine, expire_on_commit=False)
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def cleanup_tables(test_engine: Engine) -> Iterator[list[str]]:
    """Empties the tables a test dirtied, at the end (caller sets FK order)."""
    tables: list[str] = []
    try:
        yield tables
    finally:
        with test_engine.connect() as conn:
            for name in tables:
                conn.execute(text(f'DELETE FROM "{name}"'))
            conn.commit()


@pytest.fixture(scope="session", autouse=True)
def _guard_concurrent_live_runs(request: pytest.FixtureRequest) -> None:
    """Stop a second concurrent live run with a clear message.

    `run_sync` takes the 'yfin_sync' advisory lock; two overlapping live
    runs make the second fail with LockNotAcquired, which shows up as
    ERROR in file fixtures and inconsistent row counts in tests -- looks
    like a code bug. This happened once: a live run started while another
    was already in progress and produced 5 failed + 18 error, none of it
    a real regression.

    The guard only runs when live tests are actually selected. That's
    decided from the collected tests, not the `-m` expression text:
    `"live" in markexpr` would also be true for `-m "not live"` and would
    needlessly block the unit run.
    """
    if not any(item.get_closest_marker("live") for item in request.session.items):
        return
    from sqlalchemy import create_engine

    from yfin.core.config import get_settings
    from yfin.storage.db import SYNC_LOCK_NAME, lock_holder

    # Connects to the live database, not the test database. PostgreSQL
    # advisory locks are database-scoped (unlike MySQL's server-wide
    # GET_LOCK): `run_sync` takes its lock on the live database, so the
    # guard must check there too. Checking the test database would never
    # see the lock and the guard would silently do nothing.
    engine = create_engine(get_settings().db_url())
    try:
        with engine.connect() as conn:
            holder = lock_holder(conn, SYNC_LOCK_NAME)
    except Exception:  # noqa: BLE001 - no server means the real fixture already skips
        return
    finally:
        engine.dispose()

    if holder is not None:
        pytest.exit(
            f"'{SYNC_LOCK_NAME}' advisory lock is held ({holder}). "
            "Another sync or live test run is in progress; live tests cannot run "
            "concurrently. Wait for it to finish first.",
            returncode=1,
        )


def pytest_addoption(parser: pytest.Parser) -> None:
    """`--snapshot-update` rewrites the captured API examples.

    A flag rather than an environment variable so it shows up in `--help`
    next to the suite that uses it: the published examples are locked like
    `openapi.json` is, and a lock nobody knows how to update is one people
    delete.
    """
    parser.addoption(
        "--snapshot-update",
        action="store_true",
        default=False,
        help="Rewrite the committed API examples from real responses.",
    )
