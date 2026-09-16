"""Security boundary: `ENV_ONLY_FIELDS` cannot be overridden from the DB, or write access
to the `settings` table could redirect `db_host` or replace `yf_proxy_secret_key`."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from yfin.core import config as config_mod
from yfin.core.config import ENV_ONLY_FIELDS, SETTINGS_SOURCE_VAR, get_settings
from yfin.storage import settings_store
from yfin.storage.settings_store import SettingRejected, filter_overrides, validate_pair


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(SETTINGS_SOURCE_VAR, raising=False)
    config_mod.reset_settings()
    yield
    config_mod.reset_settings()


def test_a_db_host_row_is_NOT_APPLIED(monkeypatch: pytest.MonkeyPatch) -> None:
    warnings: list[dict[str, Any]] = []
    monkeypatch.setattr(
        settings_store.log, "warning", lambda msg, **kw: warnings.append(kw)
    )
    monkeypatch.setattr(
        settings_store,
        "fetch_rows",
        lambda settings: {"db_host": "evil.example.com", "yf_max_shards": "9"},
    )
    settings = get_settings()
    assert settings.db_host != "evil.example.com"
    assert settings.yf_max_shards == 9, "a legitimate override must not be dropped along with it"
    assert {kw["setting_key"] for kw in warnings} == {"db_host"}


@pytest.mark.parametrize("key", sorted(ENV_ONLY_FIELDS))
def test_every_env_only_field_is_filtered_out(key: str) -> None:
    assert filter_overrides({key: "x"}) == {}


@pytest.mark.parametrize("key", sorted(ENV_ONLY_FIELDS))
def test_the_write_path_rejects_it_too(key: str) -> None:
    """The read filter alone is not enough: a rejected row would still sit
    in the table, and `config list` would not surface it. Rejecting at
    write time ensures the row never exists at all."""
    with pytest.raises(SettingRejected):
        validate_pair(key, "x", overrides={})
