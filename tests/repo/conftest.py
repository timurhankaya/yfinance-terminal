"""Repo-test-only fixtures (`settings` layer).

`load_overrides` and `set_setting` build their own engine (not
`create_db_engine`). So the only way to point them at the test schema is
to change the bootstrap `Settings` they receive; the `settings` parameter
of `load_overrides(settings)` exists in the contract for exactly this.
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

    Two things are needed, and the second is easy to miss:

      1. The right database -- `settings.db_name` is switched to the test
         database, via the `settings` parameter of `load_overrides(settings)`.
      2. The right schema -- tests run in a process-specific PG schema
         (`tests/conftest.py`), but the production `settings_store._engine`
         doesn't set `search_path` and looks at `public`. Without this
         patch these tests can't find the `settings` table and would pass
         for the wrong reason, via a "table missing" branch.

    Patching here rather than adding a `schema` parameter to production
    code, since tests would be its only caller.
    """

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
    """Turns on the DB layer and resets the settings singleton.

    `conftest.py` sets YF_SETTINGS_SOURCE=env to prevent connecting to the
    production schema. Tests exercising the DB path must remove it --
    and without the reset, the singleton stays populated from a previous
    test, `load_overrides` never gets called, and the test passes for the
    wrong reason.
    """
    monkeypatch.delenv(SETTINGS_SOURCE_VAR, raising=False)
    config_mod.reset_settings()
    yield
    config_mod.reset_settings()
