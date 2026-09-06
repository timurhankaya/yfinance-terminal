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


def test_set_get_gidis_donusu(cli: CliRunner, test_engine: Engine) -> None:
    assert cli.invoke(config_app, ["set", "yf_max_shards", "9"]).exit_code == 0
    result = cli.invoke(config_app, ["get", "yf_max_shards"])
    assert result.exit_code == 0
    assert "yf_max_shards = 9" in result.stdout
    assert "[db]" in result.stdout


def test_anahtar_env_bicimiyle_de_kabul_edilir(cli: CliRunner, test_engine: Engine) -> None:
    """An operator used to `.env` will write `YF_MAX_SHARDS`; rejecting that
    is needless friction. The row is opened under the canonical name."""
    assert cli.invoke(config_app, ["set", " YF_MAX_SHARDS ", "9"]).exit_code == 0
    assert _rows(test_engine) == {"yf_max_shards": "9"}


def test_gecersiz_deger_cikis_2_ve_SATIR_YAZILMAZ(
    cli: CliRunner, test_engine: Engine
) -> None:
    """Feedback happens at write time; the error does not surface in
    the next night's cron run."""
    result = cli.invoke(config_app, ["set", "yf_max_shards", "0"])
    assert result.exit_code == 2
    assert _rows(test_engine) == {}


def test_env_only_anahtar_cikis_2(cli: CliRunner, test_engine: Engine) -> None:
    assert cli.invoke(config_app, ["set", "db_host", "evil"]).exit_code == 2
    assert _rows(test_engine) == {}


def test_bilinmeyen_anahtar_cikis_2(cli: CliRunner, test_engine: Engine) -> None:
    assert cli.invoke(config_app, ["set", "hicboyle_yok", "1"]).exit_code == 2
    assert _rows(test_engine) == {}


def test_unset_var_olmayan_satirda_cikis_0(cli: CliRunner) -> None:
    """Idempotent: deleting a row that does not exist is not an error."""
    result = cli.invoke(config_app, ["unset", "yf_max_shards"])
    assert result.exit_code == 0
    assert "zaten satir yoktu" in result.stdout


def test_unset_sonrasi_gecerli_olacak_deger_BASILIR(
    cli: CliRunner, test_engine: Engine
) -> None:
    cli.invoke(config_app, ["set", "yf_max_shards", "9"])
    result = cli.invoke(config_app, ["unset", "yf_max_shards"])
    assert result.exit_code == 0
    assert "artik gecerli olacak deger: 4" in result.stdout
    assert _rows(test_engine) == {}


def test_list_changed_ETKIN_DEGERI_sorar(cli: CliRunner) -> None:
    """`--changed` means "the effective value differs from the model default",
    not "the row exists". A row that writes the same value as the default is
    invisible under `--changed` but shows up under `--source db`."""
    cli.invoke(config_app, ["set", "yf_max_shards", "4"])  # same as the default
    changed = cli.invoke(config_app, ["list", "--changed"])
    assert "yf_max_shards" not in changed.stdout
    from_db = cli.invoke(config_app, ["list", "--source", "db"])
    assert "yf_max_shards" in from_db.stdout


def test_list_satirsiz_anahtarlari_ISARETLER(cli: CliRunner) -> None:
    """After migration the `.env` layer is effectively empty; a rowless key
    falls straight through to the model default."""
    result = cli.invoke(config_app, ["list", "--group", "shard"])
    assert "* yf_max_shards" in result.stdout


def test_export_varsayilan_yalniz_SATIRI_OLANLARI_verir(cli: CliRunner) -> None:
    """The natural inverse of the seed file. `--all` also includes model
    defaults and must not be checked into the repo."""
    import json

    cli.invoke(config_app, ["set", "yf_max_shards", "9"])
    result = cli.invoke(config_app, ["export"])
    assert json.loads(result.stdout) == {"yf_max_shards": 9}

    all_result = cli.invoke(config_app, ["export", "--all"])
    from yfin.core.config import DB_MANAGED_FIELDS

    assert set(json.loads(all_result.stdout)) == DB_MANAGED_FIELDS


def test_schema_DB_YE_BAKMAZ(cli: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pure schema dump runs with no network and no DB."""
    monkeypatch.setattr(
        cli_config, "fetch_rows", lambda s: pytest.fail("schema connected to the DB")
    )
    result = cli.invoke(config_app, ["schema"])
    assert result.exit_code == 0
    assert "yf_max_shards" in result.stdout


def test_source_env_iken_list_UYARIR_ve_ETKIN_degeri_gosterir(
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
    assert "DB katmani KAPALI" in result.stderr
    assert "* yf_max_shards" in result.stdout
    assert "default" in result.stdout


def test_source_env_iken_set_YINE_DE_yazar(cli: CliRunner, test_engine: Engine,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """This is exactly the recovery scenario: a broken value must be
    fixable even while the layer is off."""
    monkeypatch.setenv("YF_SETTINGS_SOURCE", "env")
    assert cli.invoke(config_app, ["set", "yf_max_shards", "9"]).exit_code == 0
    assert _rows(test_engine) == {"yf_max_shards": "9"}
