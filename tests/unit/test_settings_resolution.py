"""Resolution order and the loader's filters. `settings_store.fetch_rows` is patched rather
than `load_overrides`, so all three filters actually run."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from yfin.core import config as config_mod
from yfin.core.config import SETTINGS_SOURCE_VAR, get_settings
from yfin.storage import settings_store

# `.env` does not set this field (verified); its default is 4. Picking a
# field set in env would degrade the "DB > env" claim to "DB > default".
KEY = "yf_max_shards"
DEFAULT = 4


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test starts with a clean singleton and an open DB layer: `conftest.py` sets
    YF_SETTINGS_SOURCE=env, and without the reset the loader would never run."""
    monkeypatch.delenv(SETTINGS_SOURCE_VAR, raising=False)
    config_mod.reset_settings()
    yield
    config_mod.reset_settings()


def _rows(monkeypatch: pytest.MonkeyPatch, rows: dict[str, str] | None) -> list[int]:
    """Patch `fetch_rows` and return a call counter."""
    calls: list[int] = []

    def fake(settings: Any) -> dict[str, str] | None:
        calls.append(1)
        return rows

    monkeypatch.setattr(settings_store, "fetch_rows", fake)
    return calls


def test_a_db_override_beats_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _rows(monkeypatch, {KEY: "9"})
    assert get_settings().yf_max_shards == 9


def test_db_ezmesi_env_i_ezer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pydantic init kwargs override env; no separate precedence logic is
    written. This test pins that behavior as a contract."""
    monkeypatch.setenv("YF_MAX_SHARDS", "7")
    _rows(monkeypatch, {KEY: "9"})
    assert get_settings().yf_max_shards == 9


def test_env_applies_when_there_is_no_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YF_MAX_SHARDS", "7")
    _rows(monkeypatch, {})
    assert get_settings().yf_max_shards == 7


def test_without_the_table_the_run_continues_env_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """`yfin db upgrade` itself calls get_settings() before the table
    exists yet. This is not an error."""
    _rows(monkeypatch, None)
    assert get_settings().yf_max_shards == DEFAULT


def test_source_env_iken_db_ye_HIC_bakilmaz(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SETTINGS_SOURCE_VAR, "env")
    calls = _rows(monkeypatch, {KEY: "9"})
    assert get_settings().yf_max_shards == DEFAULT
    assert calls == [], "DB was read while YF_SETTINGS_SOURCE=env"


def test_source_env_bosluk_ve_buyuk_harf_toleransli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SETTINGS_SOURCE_VAR, "  ENV \n")
    calls = _rows(monkeypatch, {KEY: "9"})
    assert get_settings().yf_max_shards == DEFAULT
    assert calls == []


def test_a_typo_in_source_produces_a_WARNING(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recovery-purpose key silently becoming inert due to a typo is
    unacceptable: the operator would think the layer is off while it
    stays open."""
    warnings: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        config_mod.log, "warning", lambda msg, **kw: warnings.append((msg, kw))
    )
    monkeypatch.setenv(SETTINGS_SOURCE_VAR, "evn")
    _rows(monkeypatch, {KEY: "9"})
    assert get_settings().yf_max_shards == 9, "a typo must not disable the layer"
    assert warnings and "YF_SETTINGS_SOURCE" in warnings[0][0]


def test_an_unknown_key_is_ignored_with_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """`Settings` carries extra="ignore", so an unknown kwarg is silently
    swallowed; without a filter, no test would ever turn red for it."""
    warnings: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        settings_store.log, "warning", lambda msg, **kw: warnings.append((msg, kw))
    )
    _rows(monkeypatch, {"YF_MAX_SHARDS": "9", "no_such_key": "1", KEY: "6"})
    assert get_settings().yf_max_shards == 6
    reported = {kw["setting_key"] for _, kw in warnings}
    assert reported == {"YF_MAX_SHARDS", "no_such_key"}


def test_an_invalid_value_NEVER_STARTS_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A silent fallback would ignore the operator's intent, and only
    someone reading the log would notice."""
    from pydantic import ValidationError

    _rows(monkeypatch, {KEY: "0"})
    with pytest.raises(ValidationError):
        get_settings()


def test_load_overrides_get_settings_CAGIRMAZ(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guards against a RecursionError. The loader runs inside
    `get_settings()`; using a helper like `create_db_engine()` that does
    `settings or get_settings()` would create an infinite loop."""

    def boom() -> Any:
        raise AssertionError("load_overrides called get_settings()")

    _rows(monkeypatch, {KEY: "9"})
    monkeypatch.setattr(config_mod, "get_settings", boom)
    settings_store.load_overrides(config_mod.bootstrap_settings())


def test_applied_overrides_yields_the_value_to_carry_to_the_shard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parent resolves, `ShardSpec` carries it, child never re-reads it."""
    _rows(monkeypatch, {KEY: "9", "yf_news_tab": "news"})
    get_settings()
    assert config_mod.applied_overrides() == {KEY: "9", "yf_news_tab": "news"}
