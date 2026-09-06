"""Shard'lara aktarim: parent cozer, child YENIDEN OKUMAZ (CFG S3.5).

Uc sorunu birden kapatir: (a) shard'lar arasi yapilandirma carpikligi --
araya giren bir `yfin config set` shard-0 ile shard-3'u farkli
yapilandirmayla kostururdu, (b) N ekstra baglanti, (c) child
`settings.db_name`e baglanip asil isini `spec.database`de yapar, yani
YONLENDIRILMEDIGI semadan ayar okurdu.
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
    """`spawn` ile baslatilan child'a bu nesne pipe'tan gecer; picklelanamayan
    bir alan eklenmesi ancak gercek bir kosuda gorulurdu."""
    spec = _spec(settings_overrides={"yf_max_shards": "9"})
    assert pickle.loads(pickle.dumps(spec)).settings_overrides == {"yf_max_shards": "9"}


def test_child_SELECT_ATMAZ_ve_dogru_degeri_kullanir(
    test_engine: Engine,
    store_settings: Settings,
    clean_settings_table: None,
    db_layer_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Child `settings_from_overrides` + `install_settings` kullanir;
    `fetch_rows`a HIC dokunmaz."""
    from yfin.config import install_settings, settings_from_overrides

    write_all({"yf_max_shards": "9"}, settings=store_settings)
    monkeypatch.setattr(config_mod, "bootstrap_settings", lambda: store_settings)
    spec = _spec(settings_overrides={"yf_max_shards": "9"})

    config_mod.reset_settings()
    monkeypatch.setattr(
        settings_store, "fetch_rows", lambda s: pytest.fail("child DB'ye SELECT atti")
    )
    child_settings = settings_from_overrides(spec.settings_overrides)
    install_settings(child_settings)

    assert child_settings.yf_max_shards == 9
    assert get_settings() is child_settings, "install_settings singleton'i kurmali"


def test_spec_database_db_name_den_FARKLIYKEN_de_dogru_deger(
    test_engine: Engine,
    store_settings: Settings,
    clean_settings_table: None,
    db_layer_on: None,
) -> None:
    """Child `spec.database`de calisir ama ayarlari `settings.db_name`den
    okurdu -- `--database` ile yonlendirilmis bir kosuda YANLIS semadan.
    Tasima bu ayrimi tumden ortadan kaldirir."""
    from yfin.config import settings_from_overrides

    write_all({"yf_max_shards": "9"}, settings=store_settings)
    spec = _spec(settings_overrides={"yf_max_shards": "9"})
    object.__setattr__(spec, "database", "bambaska_bir_veritabani")

    assert settings_from_overrides(spec.settings_overrides).yf_max_shards == 9
