"""Transfer to shards: the parent resolves settings, the child never re-reads them.

A re-read would skew config across shards under an interleaved `yfin config
set`, open N extra connections, and read from `settings.db_name` while the
work happens in `spec.database`."""

from __future__ import annotations

import pickle

import pytest
from sqlalchemy import Engine

from yfin.core import config as config_mod
from yfin.core.config import Settings, applied_overrides, get_settings
from yfin.pipeline.shard import ShardSpec
from yfin.storage import settings_store
from yfin.storage.settings_store import write_all

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


def test_the_value_the_parent_resolved_travels_in_the_spec(
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


def test_the_spec_is_PICKLEABLE(store_settings: Settings) -> None:
    """This object passes through a pipe to a `spawn`-started child; an
    unpicklable field added here would only surface in a real run."""
    spec = _spec(settings_overrides={"yf_max_shards": "9"})
    assert pickle.loads(pickle.dumps(spec)).settings_overrides == {"yf_max_shards": "9"}


def test_the_child_ISSUES_NO_SELECT_and_uses_the_right_value(
    test_engine: Engine,
    store_settings: Settings,
    clean_settings_table: None,
    db_layer_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The child uses `settings_from_overrides` + `install_settings`;
    it never touches `fetch_rows`."""
    from yfin.core.config import install_settings, settings_from_overrides

    write_all({"yf_max_shards": "9"}, settings=store_settings)
    monkeypatch.setattr(config_mod, "bootstrap_settings", lambda: store_settings)
    spec = _spec(settings_overrides={"yf_max_shards": "9"})

    monkeypatch.setattr(config_mod, "_settings", None)
    monkeypatch.setattr(
        settings_store, "fetch_rows", lambda s: pytest.fail("child issued a SELECT to the DB")
    )
    child_settings = settings_from_overrides(spec.settings_overrides)
    install_settings(child_settings)

    assert child_settings.yf_max_shards == 9
    assert get_settings() is child_settings, "install_settings must set the singleton"


def test_the_value_is_right_even_when_spec_database_DIFFERS_from_db_name(
    test_engine: Engine,
    store_settings: Settings,
    clean_settings_table: None,
    db_layer_on: None,
) -> None:
    """The child runs in `spec.database` but would read settings from
    `settings.db_name` -- the wrong schema in a run redirected via
    `--database`. This transfer removes that distinction entirely."""
    from yfin.core.config import settings_from_overrides

    write_all({"yf_max_shards": "9"}, settings=store_settings)
    spec = _spec(settings_overrides={"yf_max_shards": "9"})
    object.__setattr__(spec, "database", "a_completely_different_database")

    assert settings_from_overrides(spec.settings_overrides).yf_max_shards == 9
