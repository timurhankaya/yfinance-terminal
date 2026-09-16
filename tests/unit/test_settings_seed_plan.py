"""The seed planner is a pure function; `seed`/`unset` interaction is tested without a DB."""

from __future__ import annotations

import pytest

from yfin.core.config import DB_MANAGED_FIELDS
from yfin.storage.settings_store import SettingRejected, adopt_env_values, plan_seed, serialize


def test_an_empty_table_gets_the_whole_JSON() -> None:
    plan = plan_seed({"yf_max_shards": 8, "yf_prune_enabled": False}, {})
    assert plan == {"yf_max_shards": "8", "yf_prune_enabled": "false"}


def test_an_existing_row_is_NOT_TOUCHED() -> None:
    plan = plan_seed({"yf_max_shards": 8}, {"yf_max_shards": "12"})
    assert plan == {}


def test_force_overwrites_a_JSON_key() -> None:
    plan = plan_seed({"yf_max_shards": 8}, {"yf_max_shards": "12"}, force=True)
    assert plan == {"yf_max_shards": "8"}


def test_force_DOES_NOT_TOUCH_a_row_OUTSIDE_the_JSON() -> None:
    """Otherwise `--force` would silently wipe every override the operator
    made from the panel."""
    plan = plan_seed({"yf_max_shards": 8}, {"yf_news_tab": "news"}, force=True)
    assert "yf_news_tab" not in plan


def test_NOTHING_is_done_for_a_key_outside_the_JSON() -> None:
    """This is what makes `unset` permanent: a key absent from the JSON
    does not get its deleted row brought back by the next `seed`."""
    plan = plan_seed({"yf_max_shards": 8}, {}, force=True)
    assert set(plan) == {"yf_max_shards"}


def test_the_key_is_normalized() -> None:
    plan = plan_seed({"  YF_MAX_SHARDS ": 8}, {})
    assert plan == {"yf_max_shards": "8"}


def test_an_unknown_key_rejects_the_WHOLE_plan() -> None:
    """All or nothing: a partially written seed would leave it ambiguous
    which key came from which source."""
    with pytest.raises(SettingRejected):
        plan_seed({"yf_max_shards": 8, "no_such_key": 1}, {})


def test_an_invalid_value_rejects_the_WHOLE_plan() -> None:
    with pytest.raises(SettingRejected):
        plan_seed({"yf_max_shards": 0}, {})


def test_an_env_only_key_is_rejected() -> None:
    """Secrets cannot enter the seed file; the file goes into the repo."""
    with pytest.raises(SettingRejected):
        plan_seed({"yf_proxy_secret_key": "abc"}, {})


def test_adopt_env_fills_only_keys_with_no_row_and_outside_the_JSON() -> None:
    env = {"yf_max_shards": "8", "yf_news_tab": "news", "yf_screen_keys": ""}
    plan = plan_seed(
        {"yf_max_shards": 4},
        {"yf_news_tab": "all"},
        adopt_env=env,
    )
    # A key present in the JSON takes the JSON value, not env's.
    assert plan["yf_max_shards"] == "4"
    # A key that already has a row is left untouched.
    assert "yf_news_tab" not in plan
    # A key with no row and absent from the JSON is inherited from env. An
    # empty string is also a legitimate value and gets written.
    assert plan["yf_screen_keys"] == ""


def test_without_adopt_env_the_scope_is_limited_to_the_JSON() -> None:
    plan = plan_seed({"yf_max_shards": 8}, {})
    assert set(plan) == {"yf_max_shards"}


def test_adopt_env_values_returns_39_keys_as_text() -> None:
    values = adopt_env_values()
    assert set(values) == DB_MANAGED_FIELDS
    assert all(isinstance(v, str) for v in values.values())


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, "true"), (False, "false"), (8, "8"), (2.0, "2.0"), ("all", "all"), ("", "")],
)
def test_serialization_rule(value: object, expected: str) -> None:
    """`"True"` is never written: pydantic would parse it too, but it would
    break round-trip equality with `.env` formatting."""
    assert serialize(value) == expected
