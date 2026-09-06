"""`load_overrides` reads the real table. Real DB."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, text

from yfin.config import Settings, get_settings
from yfin.settings_store import fetch_rows, load_overrides

pytestmark = pytest.mark.repo


def _insert(engine: Engine, key: str, value: str) -> None:
    """Writes via raw SQL -- bypassing the panel/CLI's validation.

    The read path's own defenses exist exactly for this: someone will
    always touch the table directly.
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
    _insert(test_engine, "YF_MAX_SHARDS", "9")  # non-canonical form
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
    """A silent fallback would ignore the operator's intent. The error
    happens at load time, so the run never starts."""
    _insert(test_engine, "yf_max_shards", "0")  # violates ge=1
    monkeypatch.setattr("yfin.config.bootstrap_settings", lambda: store_settings)
    with pytest.raises(ValidationError):
        get_settings()


def test_tablo_yokken_env_only_devam(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`yfin db upgrade` itself calls get_settings() before the table
    exists; that is a normal setup moment, not an error. This is also why
    `has_table` is used -- checking an error code would be engine-specific.
    """
    import yfin.settings_store as store

    monkeypatch.setattr(store, "TABLE_NAME", "settings_kesinlikle_yok")
    assert fetch_rows(settings) is None
    assert load_overrides(settings) == {}
