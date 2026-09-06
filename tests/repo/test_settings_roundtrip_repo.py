"""Round-trip guarantee: `seed(export(state)) == state`.

The serialization rule must be lossless for text/number/bool. If `bool`
were ever written as `"True"`, pydantic would still parse it, but the
export -> seed cycle would change the value, and the difference would only
surface in production.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import Engine

from yfin.core.config import DB_MANAGED_FIELDS, Settings
from yfin.storage.settings_store import (
    export_values,
    fetch_rows,
    plan_seed,
    serialize,
    settings_state,
    write_all,
)

pytestmark = pytest.mark.repo

# All four scalar types are represented.
SAMPLE = {
    "yf_max_shards": "8",  # int
    "yf_rate_limit_per_sec": "2.5",  # float
    "yf_prune_enabled": "true",  # bool
    "yf_news_tab": "news",  # str
    "yf_screen_keys": "",  # an empty string is also a legitimate value
}


def _export(settings: Settings, *, all_keys: bool) -> dict[str, object]:
    """Calls the same function as `yfin config export`.

    This logic used to be duplicated here; the copy ended up testing
    itself rather than the real output -- the same reason two separate
    validations tend to drift apart.
    """
    return export_values(settings_state(rows=fetch_rows(settings)), all_keys=all_keys)


def test_export_seed_dongusu_durumu_DEGISTIRMEZ(
    test_engine: Engine, store_settings: Settings, clean_settings_table: None
) -> None:
    write_all(SAMPLE, settings=store_settings)
    before = settings_state(rows=fetch_rows(store_settings))

    dumped = json.loads(json.dumps(_export(store_settings, all_keys=False)))
    plan = plan_seed(dumped, {}, force=True)
    write_all(plan, settings=store_settings)

    after = settings_state(rows=fetch_rows(store_settings))
    assert {k: v.value for k, v in after.items()} == {k: v.value for k, v in before.items()}


def test_export_all_returns_39_keys_and_can_be_seeded_back(
    test_engine: Engine, store_settings: Settings, clean_settings_table: None
) -> None:
    """`--all` is a backup dump kept outside the repo; it must still be
    restorable."""
    snapshot = json.loads(json.dumps(_export(store_settings, all_keys=True)))
    assert set(snapshot) == DB_MANAGED_FIELDS

    write_all(plan_seed(snapshot, {}, force=True), settings=store_settings)
    rows = fetch_rows(store_settings) or {}
    assert set(rows) == DB_MANAGED_FIELDS
    assert rows == {k: serialize(v) for k, v in snapshot.items()}


@pytest.mark.parametrize(("key", "value"), sorted(SAMPLE.items()))
def test_a_single_value_round_trips_losslessly(
    key: str, value: str, store_settings: Settings, clean_settings_table: None
) -> None:
    write_all({key: value}, settings=store_settings)
    state = settings_state(rows=fetch_rows(store_settings))[key]
    assert state.value == value
    assert state.has_row is True
