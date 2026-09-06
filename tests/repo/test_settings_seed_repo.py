"""`seed` / `unset` interaction against the real table."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text

from yfin.core.config import Settings
from yfin.storage.settings_store import (
    SettingRejected,
    fetch_rows,
    plan_seed,
    unset_setting,
    write_all,
)

pytestmark = pytest.mark.repo

SEED = {"yf_max_shards": 8, "yf_domain_regions": "US,GB", "yf_prune_enabled": False}


def _rows(engine: Engine) -> dict[str, str]:
    with engine.connect() as conn:
        return dict(conn.execute(text("SELECT setting_key, value FROM settings")).all())


def _seed_once(settings: Settings, engine: Engine, **kwargs: object) -> dict[str, str]:
    plan = plan_seed(SEED, _rows(engine), **kwargs)  # type: ignore[arg-type]
    write_all(plan, settings=settings)
    return plan


def test_a_clean_table_gets_one_row_per_JSON_key(
    test_engine: Engine, store_settings: Settings, clean_settings_table: None
) -> None:
    plan = _seed_once(store_settings, test_engine)
    assert len(plan) == len(SEED)
    assert _rows(test_engine) == {
        "yf_max_shards": "8",
        "yf_domain_regions": "US,GB",
        "yf_prune_enabled": "false",
    }


def test_the_second_run_writes_ZERO_rows(
    test_engine: Engine, store_settings: Settings, clean_settings_table: None
) -> None:
    """Idempotent: `seed` never touches an existing row."""
    _seed_once(store_settings, test_engine)
    assert _seed_once(store_settings, test_engine) == {}


def test_force_overwrites_ONLY_the_JSON_keys(
    test_engine: Engine, store_settings: Settings, clean_settings_table: None
) -> None:
    _seed_once(store_settings, test_engine)
    write_all({"yf_max_shards": "12", "yf_news_tab": "news"}, settings=store_settings)

    _seed_once(store_settings, test_engine, force=True)
    rows = _rows(test_engine)
    assert rows["yf_max_shards"] == "8", "a JSON key should have been overwritten"
    assert rows["yf_news_tab"] == "news", "a row outside JSON should not be touched"


def test_invalid_JSON_writes_NOTHING(
    test_engine: Engine, store_settings: Settings, clean_settings_table: None
) -> None:
    """All or nothing: the whole JSON is validated first, then written in
    a single transaction."""
    bad = {**SEED, "yf_screen_size": 9999}  # violates le=250
    with pytest.raises(SettingRejected):
        plan_seed(bad, _rows(test_engine))
    assert _rows(test_engine) == {}


def test_unset_is_PERMANENT_for_a_key_OUTSIDE_the_JSON(
    test_engine: Engine, store_settings: Settings, clean_settings_table: None
) -> None:
    """In v1, `seed` meant "fill every missing row" and silently undid
    `unset`; the two commands stepped on each other."""
    write_all({"yf_news_tab": "news"}, settings=store_settings)
    assert unset_setting("yf_news_tab", settings=store_settings) is True

    _seed_once(store_settings, test_engine)
    assert "yf_news_tab" not in _rows(test_engine)


def test_unset_COMES_BACK_with_seed_for_a_key_INSIDE_the_JSON(
    test_engine: Engine, store_settings: Settings, clean_settings_table: None
) -> None:
    """And this is the correct behavior: JSON is "this install's config".
    `yfin config unset` output warns about this explicitly."""
    _seed_once(store_settings, test_engine)
    assert unset_setting("yf_max_shards", settings=store_settings) is True
    _seed_once(store_settings, test_engine)
    assert _rows(test_engine)["yf_max_shards"] == "8"


def test_unset_returns_False_when_the_row_does_not_exist(
    store_settings: Settings, clean_settings_table: None
) -> None:
    """Idempotent; the CLI turns this into an exit-code-0 notice."""
    assert unset_setting("yf_news_tab", settings=store_settings) is False


def test_adopt_env_fills_in_keys_with_no_row(
    test_engine: Engine, store_settings: Settings, clean_settings_table: None
) -> None:
    """Migration step: an install with YF_MAX_SHARDS=8 in `.env` would
    silently fall back to the default after migration without this step."""
    from yfin.core.config import DB_MANAGED_FIELDS
    from yfin.storage.settings_store import adopt_env_values

    _seed_once(store_settings, test_engine, adopt_env=adopt_env_values())
    assert set(fetch_rows(store_settings) or {}) == DB_MANAGED_FIELDS
