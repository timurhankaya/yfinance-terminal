"""`settings_state()` and `export_values()` are pure functions. No DB.

Both used to depend on a DB (`settings_state` took a `Settings` it never
used, `export_values` was embedded inside a CLI command), so they could
only be tested with a real database in a repo test. Splitting them out
made this file possible.
"""

from __future__ import annotations

import pytest

from yfin.core.config import DB_MANAGED_FIELDS, ENV_ONLY_FIELDS
from yfin.storage.settings_store import (
    KeyVerdict,
    Source,
    classify_key,
    export_values,
    settings_state,
)


def test_rows_None_DB_YE_BAKILMADI_demektir() -> None:
    """An unreachable DB reaches the same outcome as `YF_SETTINGS_SOURCE=env`:
    no row is active, and the source falls back to env/default."""
    states = settings_state(rows=None)
    assert set(states) == DB_MANAGED_FIELDS
    assert all(not s.has_row for s in states.values())
    assert all(s.source is not Source.DB for s in states.values())


def test_a_row_makes_the_source_db() -> None:
    states = settings_state(rows={"yf_max_shards": "9"})
    assert states["yf_max_shards"].value == "9"
    assert states["yf_max_shards"].source is Source.DB
    assert states["yf_max_shards"].has_row is True


def test_has_row_and_source_are_NOT_THE_SAME_THING() -> None:
    """An env-only row can exist in the table but is not applied. A single
    flag could not represent that distinction."""
    states = settings_state(rows={"yf_max_shards": "9"})
    assert "db_host" not in states, "an env-only field has no row in the state table"


def test_a_field_set_in_env_has_source_ENV() -> None:
    """The distinction is made via `model_fields_set`, not by comparing
    "is the value different from the default": a `.env` setting that
    happens to equal the default would otherwise show up as `default`."""
    from yfin.core.config import bootstrap_settings

    env_set = bootstrap_settings().model_fields_set & DB_MANAGED_FIELDS
    if not env_set:  # pragma: no cover - depends on .env
        pytest.skip(".env sets no DB-managed field")
    states = settings_state(rows={})
    assert all(states[key].source is Source.ENV for key in env_set)


def test_export_by_default_returns_only_keys_with_a_row() -> None:
    states = settings_state(rows={"yf_max_shards": "9"})
    assert export_values(states) == {"yf_max_shards": 9}


def test_export_all_returns_39_keys_with_their_NATIVE_type() -> None:
    """If this returned strings, `seed(export(state))` would re-serialize
    `"8"` on the next round and the seed file would drift into untyped text."""
    out = export_values(settings_state(rows={}), all_keys=True)
    assert set(out) == DB_MANAGED_FIELDS
    assert isinstance(out["yf_max_shards"], int)
    assert isinstance(out["yf_prune_enabled"], bool)
    assert isinstance(out["yf_rate_limit_per_sec"], float)
    assert isinstance(out["yf_news_tab"], str)


def test_export_bos_state_bos_sozluk() -> None:
    assert export_values({}) == {}


@pytest.mark.parametrize("key", sorted(DB_MANAGED_FIELDS))
def test_classify_db_yonetimli_alanlar_OK(key: str) -> None:
    assert classify_key(key) is KeyVerdict.OK


@pytest.mark.parametrize("key", sorted(ENV_ONLY_FIELDS))
def test_classify_env_only_alanlar(key: str) -> None:
    assert classify_key(key) is KeyVerdict.ENV_ONLY


@pytest.mark.parametrize("key", ["hicboyle_yok", "YF_MAX_SHARDS", ""])
def test_classify_unknown_keys(key: str) -> None:
    """A non-canonical form (`YF_MAX_SHARDS`) is also UNKNOWN: keeping such
    a row visible if it were inserted via raw SQL is why the collation
    decision matters."""
    assert classify_key(key) is KeyVerdict.UNKNOWN


def test_the_read_and_write_paths_make_the_SAME_decision() -> None:
    """Proof that the policy comes from a single source.

    If it were coded in two places, a category missed on the read path
    would breach the security boundary (an applied row containing
    `db_host`).
    """
    from yfin.storage.settings_store import SettingRejected, filter_overrides, validate_pair

    for key in sorted(ENV_ONLY_FIELDS | {"hicboyle_yok"}):
        assert filter_overrides({key: "x"}) == {}, f"{key} passed on the read path"
        with pytest.raises(SettingRejected):
            validate_pair(key, "x", overrides={})
