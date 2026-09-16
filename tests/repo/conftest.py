"""Repo-test-only fixtures (`settings` layer).

`load_overrides` and `set_setting` build their own engine, so the bootstrap
`Settings` they receive is the only way to point them at the test schema.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.pool import NullPool

from yfin.core import config as config_mod
from yfin.core.config import SETTINGS_SOURCE_VAR, Settings
from yfin.storage import settings_store


@pytest.fixture
def store_settings(
    settings: Settings,
    test_engine: Engine,
    test_schema: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Settings:
    """Points the config layer at the test schema.

    `settings.db_name` alone is not enough: the production `settings_store._engine`
    never sets `search_path`, so the tests would miss the `settings` table."""

    def _schema_engine(cfg: Settings) -> Engine:
        return create_engine(
            cfg.db_url(),
            poolclass=NullPool,
            # `public` stays in the list, same reason as the production
            # `create_db_engine` path (timescaledb extension lives there).
            connect_args={"options": f"-c search_path={test_schema},public"},
        )

    monkeypatch.setattr(settings_store, "_engine", _schema_engine)
    return settings.model_copy(update={"db_name": test_engine.url.database})


@pytest.fixture
def clean_settings_table(test_engine: Engine) -> Iterator[None]:
    """Each test starts with an empty table and cleans up after itself.

    Row count in this table is not invariant; a row left over from a
    previous test would silently falsify a "seed wrote N rows" assertion.
    """
    with test_engine.connect() as conn:
        conn.execute(text("DELETE FROM settings"))
        conn.commit()
    yield
    with test_engine.connect() as conn:
        conn.execute(text("DELETE FROM settings"))
        conn.commit()


@pytest.fixture
def db_layer_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Turns on the DB layer and clears the settings singleton.

    Without the clear the singleton stays populated from a previous test and
    `load_overrides` is never called.
    """
    monkeypatch.delenv(SETTINGS_SOURCE_VAR, raising=False)
    monkeypatch.setattr(config_mod, "_settings", None)
    monkeypatch.setattr(config_mod, "_overrides", {})
    yield
