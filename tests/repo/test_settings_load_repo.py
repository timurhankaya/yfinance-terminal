"""`load_overrides` gercek tabloyu okur (CFG S8.2). Gercek DB."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, text

from yfin.config import Settings, get_settings
from yfin.settings_store import fetch_rows, load_overrides

pytestmark = pytest.mark.repo


def _insert(engine: Engine, key: str, value: str) -> None:
    """HAM SQL ile yazar -- yani panelin/CLI'nin dogrulamasini ATLAYARAK.

    Okuma yolunun kendi savunmalari tam olarak bu senaryo icin vardir:
    tabloya dogrudan dokunan biri her zaman olacaktir.
    """
    with engine.connect() as conn:
        conn.execute(
            text("INSERT INTO settings (setting_key, value) VALUES (:k, :v)"),
            {"k": key, "v": value},
        )
        conn.commit()


def test_gercek_satir_okunur(
    test_engine: Engine, store_settings: Settings, clean_settings_table: None
) -> None:
    _insert(test_engine, "yf_max_shards", "9")
    assert load_overrides(store_settings) == {"yf_max_shards": "9"}


def test_bilinmeyen_ve_env_only_satirlar_DUSURULUR(
    test_engine: Engine, store_settings: Settings, clean_settings_table: None
) -> None:
    _insert(test_engine, "YF_MAX_SHARDS", "9")  # kanonik olmayan bicim
    _insert(test_engine, "db_host", "evil.example.com")
    _insert(test_engine, "yf_news_tab", "news")
    assert load_overrides(store_settings) == {"yf_news_tab": "news"}


def test_ham_SQL_ile_sokulan_bozuk_deger_kosuyu_COKTURUR(
    test_engine: Engine,
    store_settings: Settings,
    clean_settings_table: None,
    db_layer_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sessiz geri dusus operatorun niyetini yok sayardi (CFG S7).
    Hata YUKLEME aninda dogar, yani kosu HIC baslamaz."""
    _insert(test_engine, "yf_max_shards", "0")  # ge=1 ihlali
    monkeypatch.setattr("yfin.config.bootstrap_settings", lambda: store_settings)
    with pytest.raises(ValidationError):
        get_settings()


def test_tablo_yokken_env_only_devam(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`yfin db upgrade` komutunun KENDISI get_settings() cagiriyor ve
    tablo o an henuz yoktur; bu bir hata degil normal bir kurulum
    anidir. `has_table` kullanilmasinin gerekcesi de budur -- hata
    koduna (1146 / 42P01) bakmak motora bagimli olurdu."""
    import yfin.settings_store as store

    monkeypatch.setattr(store, "TABLE_NAME", "settings_kesinlikle_yok")
    assert fetch_rows(settings) is None
    assert load_overrides(settings) == {}
