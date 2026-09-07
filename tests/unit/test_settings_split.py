"""Four guards on the field split.

`DB_MANAGED_FIELDS` is not a hand-written list, it is the complement of
`ENV_ONLY_FIELDS`. This makes it impossible for a new field to silently
fall into neither set -- but the tradeoff is fail-open: a new field is
exposed to the DB by default. The four guards here close that gap.
"""

from __future__ import annotations

import re

import pytest

from yfin.core.config import (
    DB_MANAGED_FIELDS,
    ENV_ONLY_FIELDS,
    SETTING_GROUPS,
    Settings,
)

# Numbers are a 2026-09-07 snapshot (the live stream added 17 fields in the
# new `stream` group) and the mechanism does not depend on them; they are
# kept so that adding a field forces this file to be read.
SNAPSHOT_TOTAL = 63
SNAPSHOT_ENV_ONLY = 8
SNAPSHOT_DB_MANAGED = 55

SECRET_NAME_RE = re.compile(r"secret|password|token|credential")


def test_the_sets_are_exhaustive_and_disjoint() -> None:
    """Guard 1 -- exhaustiveness.

    Every new `Settings` field always falls into one of the two sets; a
    field in neither cannot exist.
    """
    assert set(Settings.model_fields) == ENV_ONLY_FIELDS | DB_MANAGED_FIELDS
    assert not (ENV_ONLY_FIELDS & DB_MANAGED_FIELDS)


def test_anlik_goruntu_sayilari() -> None:
    assert len(Settings.model_fields) == SNAPSHOT_TOTAL
    assert len(ENV_ONLY_FIELDS) == SNAPSHOT_ENV_ONLY
    assert len(DB_MANAGED_FIELDS) == SNAPSHOT_DB_MANAGED


@pytest.mark.parametrize("key", sorted(DB_MANAGED_FIELDS))
def test_metadata_eksiksiz(key: str) -> None:
    """Guard 2 -- metadata.

    The panel form cannot render without `description` + `group`; a field
    missing either would show up nameless and ungrouped in the panel.
    """
    info = Settings.model_fields[key]
    assert info.description, f"{key}: missing description"
    extra = info.json_schema_extra
    assert isinstance(extra, dict), f"{key}: missing json_schema_extra"
    assert extra.get("group") in SETTING_GROUPS, f"{key}: invalid group {extra.get('group')!r}"


def test_gruplar_hepsi_kullaniliyor() -> None:
    """An unused group name would leave the panel with an empty tab."""
    used = {
        Settings.model_fields[key].json_schema_extra["group"]  # type: ignore[index]
        for key in DB_MANAGED_FIELDS
    }
    assert used == set(SETTING_GROUPS)


@pytest.mark.parametrize("key", sorted(Settings.model_fields))
def test_sir_adi_citi(key: str) -> None:
    """Guard 3 -- secret-name pattern.

    If a field whose name matches `secret|password|token|credential` is not
    in `ENV_ONLY_FIELDS`, the complement derivation would expose it to the
    DB, putting the secret next to the data it protects.
    """
    if SECRET_NAME_RE.search(key):
        assert key in ENV_ONLY_FIELDS, f"{key} is named like a secret but is DB-managed"


@pytest.mark.parametrize("key", sorted(DB_MANAGED_FIELDS))
def test_skaler_citi(key: str) -> None:
    """Guard 4 -- scalar-ness.

    The `value` column is text, and the serialization rule is defined only
    for scalars. If a field ever becomes a list/dict, this test breaks and
    forces the rule to be updated.
    """
    assert Settings.model_fields[key].annotation in (bool, int, float, str)
