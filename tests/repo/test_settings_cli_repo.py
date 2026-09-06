"""Round-trip of `yfin config` commands. Real DB.

Commands connect via `bootstrap_settings()`; tests point it at the test
schema. The patch is deliberately on `cli_config`: functions in
`settings_store` take `settings` as a parameter from the command, so which
Settings the command passes is also under test.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from typer.testing import CliRunner

from yfin.cli import settings as cli_config
from yfin.cli.settings import config_app
from yfin.core.config import Settings

pytestmark = pytest.mark.repo

runner = CliRunner()


@pytest.fixture
def cli(
    store_settings: Settings,
    clean_settings_table: None,
    db_layer_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[CliRunner]:
    monkeypatch.setattr(cli_config, "bootstrap_settings", lambda: store_settings)
    yield runner


def _rows(engine: Engine) -> dict[str, str]:
    with engine.connect() as conn:
        return dict(conn.execute(text("SELECT setting_key, value FROM settings")).all())


def test_set_get_round_trip(cli: CliRunner, test_engine: Engine) -> None:
    assert cli.invoke(config_app, ["set", "yf_max_shards", "9"]).exit_code == 0
    result = cli.invoke(config_app, ["get", "yf_max_shards"])
    assert result.exit_code == 0
    assert "yf_max_shards = 9" in result.stdout
    assert "[db]" in result.stdout


def test_a_key_is_also_accepted_in_env_form(cli: CliRunner, test_engine: Engine) -> None:
    """An operator used to `.env` will write `YF_MAX_SHARDS`; rejecting that
    is needless friction. The row is opened under the canonical name."""
    assert cli.invoke(config_app, ["set", " YF_MAX_SHARDS ", "9"]).exit_code == 0
    assert _rows(test_engine) == {"yf_max_shards": "9"}


def test_an_invalid_value_exits_2_and_WRITES_NO_ROW(
    cli: CliRunner, test_engine: Engine
) -> None:
    """Feedback happens at write time; the error does not surface in
    the next night's cron run."""
    result = cli.invoke(config_app, ["set", "yf_max_shards", "0"])
    assert result.exit_code == 2
    assert _rows(test_engine) == {}


def test_an_env_only_key_exits_2(cli: CliRunner, test_engine: Engine) -> None:
    assert cli.invoke(config_app, ["set", "db_host", "evil"]).exit_code == 2
    assert _rows(test_engine) == {}


def test_an_unknown_key_exits_2(cli: CliRunner, test_engine: Engine) -> None:
    assert cli.invoke(config_app, ["set", "no_such_key", "1"]).exit_code == 2
    assert _rows(test_engine) == {}


def test_unset_exits_0_when_the_row_does_not_exist(cli: CliRunner) -> None:
    """Idempotent: deleting a row that does not exist is not an error."""
    result = cli.invoke(config_app, ["unset", "yf_max_shards"])
    assert result.exit_code == 0
    assert "there was no row" in result.stdout


def test_unset_PRINTS_the_value_that_will_take_effect(
    cli: CliRunner, test_engine: Engine
) -> None:
    cli.invoke(config_app, ["set", "yf_max_shards", "9"])
    result = cli.invoke(config_app, ["unset", "yf_max_shards"])
    assert result.exit_code == 0
    assert "value now in effect: 4" in result.stdout
    assert _rows(test_engine) == {}


def test_list_changed_asks_about_the_EFFECTIVE_VALUE(cli: CliRunner) -> None:
    """`--changed` means "the effective value differs from the model default",
    not "the row exists". A row that writes the same value as the default is
    invisible under `--changed` but shows up under `--source db`."""
    cli.invoke(config_app, ["set", "yf_max_shards", "4"])  # same as the default
    changed = cli.invoke(config_app, ["list", "--changed"])
    assert "yf_max_shards" not in changed.stdout
    from_db = cli.invoke(config_app, ["list", "--source", "db"])
    assert "yf_max_shards" in from_db.stdout


def test_list_MARKS_keys_with_no_row(cli: CliRunner) -> None:
    """After migration the `.env` layer is effectively empty; a rowless key
    falls straight through to the model default."""
    result = cli.invoke(config_app, ["list", "--group", "shard"])
    assert "* yf_max_shards" in result.stdout


def test_export_by_default_returns_ONLY_KEYS_WITH_A_ROW(cli: CliRunner) -> None:
    """The natural inverse of the seed file. `--all` also includes model
    defaults and must not be checked into the repo."""
    import json

    cli.invoke(config_app, ["set", "yf_max_shards", "9"])
    result = cli.invoke(config_app, ["export"])
    assert json.loads(result.stdout) == {"yf_max_shards": 9}

    all_result = cli.invoke(config_app, ["export", "--all"])
    from yfin.core.config import DB_MANAGED_FIELDS

    assert set(json.loads(all_result.stdout)) == DB_MANAGED_FIELDS


def test_schema_DOES_NOT_TOUCH_THE_DB(cli: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pure schema dump runs with no network and no DB."""
    monkeypatch.setattr(
        cli_config, "fetch_rows", lambda s: pytest.fail("schema connected to the DB")
    )
    result = cli.invoke(config_app, ["schema"])
    assert result.exit_code == 0
    assert "yf_max_shards" in result.stdout


def test_with_source_env_list_WARNS_and_shows_the_EFFECTIVE_value(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While the DB layer is off, the row in the table is not effective;
    `list` prints the value that is actually in effect instead of showing
    it as active, with a warning in the header. Otherwise an operator
    recovering the system would read values from a layer they believe
    is disabled."""
    cli.invoke(config_app, ["set", "yf_max_shards", "9"])
    monkeypatch.setenv("YF_SETTINGS_SOURCE", "env")

    result = cli.invoke(config_app, ["list", "--group", "shard"])
    assert result.exit_code == 0
    assert "DB layer OFF" in result.stderr
    assert "* yf_max_shards" in result.stdout
    assert "default" in result.stdout


def test_with_source_env_set_STILL_WRITES(cli: CliRunner, test_engine: Engine,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """This is exactly the recovery scenario: a broken value must be
    fixable even while the layer is off."""
    monkeypatch.setenv("YF_SETTINGS_SOURCE", "env")
    assert cli.invoke(config_app, ["set", "yf_max_shards", "9"]).exit_code == 0
    assert _rows(test_engine) == {"yf_max_shards": "9"}
