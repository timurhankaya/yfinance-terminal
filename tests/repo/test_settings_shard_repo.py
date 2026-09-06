"""Transfer to shards: the parent resolves settings, the child never re-reads them.

Closes three problems at once: (a) cross-shard config skew -- an
interleaved `yfin config set` would run shard-0 and shard-3 under
different configs, (b) N extra connections, (c) the child connects to
`settings.db_name` but does its real work in `spec.database`, i.e. it
would read settings from a schema it was not redirected to.
"""

from __future__ import annotations

import pickle

import pytest
from sqlalchemy import Engine

from yfin import config as config_mod
from yfin import settings_store
from yfin.config import Settings, applied_overrides, get_settings
from yfin.settings_store import write_all
from yfin.shard import ShardSpec

pytestmark = pytest.mark.repo


def _spec(**kwargs: object) -> ShardSpec:
    return ShardSpec(
        run_id=1,
        shard_index=0,
        dataset_names=("symbols",),
        full_refresh=False,
        database=None,
        **kwargs,  # type: ignore[arg-type]
    )


def test_parent_in_cozdugu_deger_spec_ile_tasinir(
    test_engine: Engine,
    store_settings: Settings,
    clean_settings_table: None,
    db_layer_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_all({"yf_max_shards": "9", "yf_news_tab": "news"}, settings=store_settings)
    monkeypatch.setattr(config_mod, "bootstrap_settings", lambda: store_settings)

    assert get_settings().yf_max_shards == 9
    spec = _spec(settings_overrides=applied_overrides())
    assert spec.settings_overrides == {"yf_max_shards": "9", "yf_news_tab": "news"}


def test_spec_PICKLE_lanabilir(store_settings: Settings) -> None:
    """This object passes through a pipe to a `spawn`-started child; an
    unpicklable field added here would only surface in a real run."""
    spec = _spec(settings_overrides={"yf_max_shards": "9"})
    assert pickle.loads(pickle.dumps(spec)).settings_overrides == {"yf_max_shards": "9"}


def test_child_SELECT_ATMAZ_ve_dogru_degeri_kullanir(
    test_engine: Engine,
    store_settings: Settings,
    clean_settings_table: None,
    db_layer_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The child uses `settings_from_overrides` + `install_settings`;
    it never touches `fetch_rows`."""
    from yfin.config import install_settings, settings_from_overrides

    write_all({"yf_max_shards": "9"}, settings=store_settings)
    monkeypatch.setattr(config_mod, "bootstrap_settings", lambda: store_settings)
    spec = _spec(settings_overrides={"yf_max_shards": "9"})

    config_mod.reset_settings()
    monkeypatch.setattr(
        settings_store, "fetch_rows", lambda s: pytest.fail("child issued a SELECT to the DB")
    )
    child_settings = settings_from_overrides(spec.settings_overrides)
    install_settings(child_settings)

    assert child_settings.yf_max_shards == 9
    assert get_settings() is child_settings, "install_settings must set the singleton"


def test_spec_database_db_name_den_FARKLIYKEN_de_dogru_deger(
    test_engine: Engine,
    store_settings: Settings,
    clean_settings_table: None,
    db_layer_on: None,
) -> None:
    """The child runs in `spec.database` but would read settings from
    `settings.db_name` -- the wrong schema in a run redirected via
    `--database`. This transfer removes that distinction entirely."""
    from yfin.config import settings_from_overrides

    write_all({"yf_max_shards": "9"}, settings=store_settings)
    spec = _spec(settings_overrides={"yf_max_shards": "9"})
    object.__setattr__(spec, "database", "bambaska_bir_veritabani")

    assert settings_from_overrides(spec.settings_overrides).yf_max_shards == 9
