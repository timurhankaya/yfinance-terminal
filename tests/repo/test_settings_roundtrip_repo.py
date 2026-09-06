"""Gidis-donus garantisi: `seed(export(state)) == state` (CFG S4.4/S8.2).

Serilestirme kurali metin/sayi/bool icin KAYIPSIZ olmak zorundadir. Bir
gun `bool` icin `"True"` yazilsaydi pydantic onu yine cozerdi ama
`export` -> `seed` dongusu degeri degistirir ve fark ancak uretimde
gorulurdu.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import Engine

from yfin.config import DB_MANAGED_FIELDS, Settings
from yfin.settings_store import (
    export_values,
    fetch_rows,
    plan_seed,
    serialize,
    settings_state,
    write_all,
)

pytestmark = pytest.mark.repo

# Dort skaler tipin dordu de temsil edilir (CFG S4.4).
SAMPLE = {
    "yf_max_shards": "8",  # int
    "yf_rate_limit_per_sec": "2.5",  # float
    "yf_prune_enabled": "true",  # bool
    "yf_news_tab": "news",  # str
    "yf_screen_keys": "",  # bos dize DE mesru bir degerdir
}


def _export(settings: Settings, *, all_keys: bool) -> dict[str, object]:
    """`yfin config export` ile AYNI fonksiyonu cagirir.

    Once bu mantik burada KOPYALANMISTI; kopya, ciktinin dogrulugunu
    degil kendi kendini sinar hale gelmisti (CFG S6.3'un "iki ayri
    dogrulama yazilsaydi biri gevserdi" gerekcesinin aynisi).
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


def test_export_all_39_anahtari_verir_ve_geri_tohumlanabilir(
    test_engine: Engine, store_settings: Settings, clean_settings_table: None
) -> None:
    """`--all` yedek ciktisidir (CFG S9) ve DEPO DISINA alinir; yine de
    geri yuklenebilir olmak zorundadir."""
    snapshot = json.loads(json.dumps(_export(store_settings, all_keys=True)))
    assert set(snapshot) == DB_MANAGED_FIELDS

    write_all(plan_seed(snapshot, {}, force=True), settings=store_settings)
    rows = fetch_rows(store_settings) or {}
    assert set(rows) == DB_MANAGED_FIELDS
    assert rows == {k: serialize(v) for k, v in snapshot.items()}


@pytest.mark.parametrize(("key", "value"), sorted(SAMPLE.items()))
def test_tek_deger_kayipsiz_gider_gelir(
    key: str, value: str, store_settings: Settings, clean_settings_table: None
) -> None:
    write_all({key: value}, settings=store_settings)
    state = settings_state(rows=fetch_rows(store_settings))[key]
    assert state.value == value
    assert state.has_row is True
