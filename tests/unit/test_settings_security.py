"""Guvenlik siniri: `ENV_ONLY_FIELDS` DB'den EZILEMEZ (CFG S3.2/S7).

Bu filtre bir "iyi olur" degildir. Uygulanabilseydi `settings` tablosuna
yazma yetkisi olan biri `db_host`u degistirip TUM baglantiyi baska bir
sunucuya cevirebilir ya da `yf_proxy_secret_key`i ezip proxy
parolalarinin cozumunu ele gecirebilirdi.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from yfin import config as config_mod
from yfin import settings_store
from yfin.config import ENV_ONLY_FIELDS, SETTINGS_SOURCE_VAR, get_settings
from yfin.settings_store import SettingRejected, filter_overrides, validate_pair


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(SETTINGS_SOURCE_VAR, raising=False)
    config_mod.reset_settings()
    yield
    config_mod.reset_settings()


def test_db_host_satiri_UYGULANMAZ(monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert settings.yf_max_shards == 9, "mesru ezme de birlikte dusmemeli"
    assert {kw["setting_key"] for kw in warnings} == {"db_host"}


@pytest.mark.parametrize("key", sorted(ENV_ONLY_FIELDS))
def test_her_env_only_alan_filtrelenir(key: str) -> None:
    assert filter_overrides({key: "x"}) == {}


@pytest.mark.parametrize("key", sorted(ENV_ONLY_FIELDS))
def test_yazma_yolu_da_reddeder(key: str) -> None:
    """Okuma filtresi TEK basina yetmez: reddedilen satir yine de
    tabloda durur ve `config list` onu gorunur kilmaz. Yazim aninda
    reddetmek satirin HIC olusmamasini saglar."""
    with pytest.raises(SettingRejected):
        validate_pair(key, "x", overrides={})
