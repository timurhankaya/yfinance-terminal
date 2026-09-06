"""`seed` / `unset` etkilesimi gercek tablo uzerinde (CFG S5.2/S5.3)."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text

from yfin.config import Settings
from yfin.settings_store import (
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


def test_temiz_tabloda_JSON_kadar_satir_yazilir(
    test_engine: Engine, store_settings: Settings, clean_settings_table: None
) -> None:
    plan = _seed_once(store_settings, test_engine)
    assert len(plan) == len(SEED)
    assert _rows(test_engine) == {
        "yf_max_shards": "8",
        "yf_domain_regions": "US,GB",
        "yf_prune_enabled": "false",
    }


def test_ikinci_kosu_SIFIR_satir_yazar(
    test_engine: Engine, store_settings: Settings, clean_settings_table: None
) -> None:
    """Idempotent: `seed` var olan satira DOKUNMAZ."""
    _seed_once(store_settings, test_engine)
    assert _seed_once(store_settings, test_engine) == {}


def test_force_yalniz_JSON_anahtarlarini_ezer(
    test_engine: Engine, store_settings: Settings, clean_settings_table: None
) -> None:
    _seed_once(store_settings, test_engine)
    write_all({"yf_max_shards": "12", "yf_news_tab": "news"}, settings=store_settings)

    _seed_once(store_settings, test_engine, force=True)
    rows = _rows(test_engine)
    assert rows["yf_max_shards"] == "8", "JSON anahtari ezilmeliydi"
    assert rows["yf_news_tab"] == "news", "JSON DISI satira dokunulmamaliydi"


def test_gecersiz_JSON_da_HICBIR_SEY_yazilmaz(
    test_engine: Engine, store_settings: Settings, clean_settings_table: None
) -> None:
    """YA HEP YA HIC (CFG S5.2): once JSON'un tamami dogrulanir, sonra
    tek transaction'da yazilir."""
    bad = {**SEED, "yf_screen_size": 9999}  # le=250 ihlali
    with pytest.raises(SettingRejected):
        plan_seed(bad, _rows(test_engine))
    assert _rows(test_engine) == {}


def test_unset_JSON_DISI_anahtarda_KALICIDIR(
    test_engine: Engine, store_settings: Settings, clean_settings_table: None
) -> None:
    """v1'de `seed` "tum eksik satirlari doldur" idi ve `unset`i SESSIZCE
    geri aliyordu; iki komut birbirinin isini bozuyordu (CFG S5.3)."""
    write_all({"yf_news_tab": "news"}, settings=store_settings)
    assert unset_setting("yf_news_tab", settings=store_settings) is True

    _seed_once(store_settings, test_engine)
    assert "yf_news_tab" not in _rows(test_engine)


def test_unset_JSON_ICI_anahtarda_seed_ile_GERI_GELIR(
    test_engine: Engine, store_settings: Settings, clean_settings_table: None
) -> None:
    """Ve bu DOGRU davranistir: JSON "bu kurulumun yapilandirmasi"dir.
    `yfin config unset` ciktisi bunu acikca uyarir."""
    _seed_once(store_settings, test_engine)
    assert unset_setting("yf_max_shards", settings=store_settings) is True
    _seed_once(store_settings, test_engine)
    assert _rows(test_engine)["yf_max_shards"] == "8"


def test_unset_var_olmayan_satirda_False_doner(
    store_settings: Settings, clean_settings_table: None
) -> None:
    """Idempotent; CLI bunu cikis kodu 0 ile bilgilendirmeye cevirir."""
    assert unset_setting("yf_news_tab", settings=store_settings) is False


def test_adopt_env_satirsiz_anahtarlari_doldurur(
    test_engine: Engine, store_settings: Settings, clean_settings_table: None
) -> None:
    """Goc adimi (CFG S5.2/S9): `.env`inde YF_MAX_SHARDS=8 olan bir
    kurulum bu adim olmadan migration sonrasi SESSIZCE varsayilana
    donerdi."""
    from yfin.config import DB_MANAGED_FIELDS
    from yfin.settings_store import adopt_env_values

    _seed_once(store_settings, test_engine, adopt_env=adopt_env_values())
    assert set(fetch_rows(store_settings) or {}) == DB_MANAGED_FIELDS
