"""Tohum planlayicisi SAF bir fonksiyondur (CFG S5.2). DB YOK.

Planlayicinin ayri ve saf olmasi, `seed` ile `unset` etkilesiminin
(CFG S5.3) agsiz sinanabilmesi icindir -- v1'de bu iki komut birbirinin
isini bozuyordu ve hata ancak gercek bir DB'de gorulebilirdi.
"""

from __future__ import annotations

import pytest

from yfin.config import DB_MANAGED_FIELDS
from yfin.settings_store import SettingRejected, adopt_env_values, plan_seed, serialize


def test_bos_tabloda_JSON_un_tamami_yazilir() -> None:
    plan = plan_seed({"yf_max_shards": 8, "yf_prune_enabled": False}, {})
    assert plan == {"yf_max_shards": "8", "yf_prune_enabled": "false"}


def test_var_olan_satira_DOKUNULMAZ() -> None:
    plan = plan_seed({"yf_max_shards": 8}, {"yf_max_shards": "12"})
    assert plan == {}


def test_force_JSON_anahtarini_ezer() -> None:
    plan = plan_seed({"yf_max_shards": 8}, {"yf_max_shards": "12"}, force=True)
    assert plan == {"yf_max_shards": "8"}


def test_force_JSON_DISI_satira_DOKUNMAZ() -> None:
    """Aksi halde `--force`, operatorun panelden yaptigi TUM ezmeleri
    sessizce silerdi."""
    plan = plan_seed({"yf_max_shards": 8}, {"yf_news_tab": "news"}, force=True)
    assert "yf_news_tab" not in plan


def test_JSON_disi_anahtar_icin_HICBIR_SEY_yapilmaz() -> None:
    """`unset`i KALICI kilan kural budur (CFG S5.3): JSON'da olmayan bir
    anahtarin silinmis satiri bir sonraki `seed` ile GERI GELMEZ."""
    plan = plan_seed({"yf_max_shards": 8}, {}, force=True)
    assert set(plan) == {"yf_max_shards"}


def test_anahtar_normalize_edilir() -> None:
    plan = plan_seed({"  YF_MAX_SHARDS ": 8}, {})
    assert plan == {"yf_max_shards": "8"}


def test_bilinmeyen_anahtar_TUM_plani_reddeder() -> None:
    """YA HEP YA HIC: yarim yazilmis bir tohum, hangi anahtarin hangi
    kaynaktan geldigini belirsiz birakirdi."""
    with pytest.raises(SettingRejected):
        plan_seed({"yf_max_shards": 8, "hicboyle_yok": 1}, {})


def test_gecersiz_deger_TUM_plani_reddeder() -> None:
    with pytest.raises(SettingRejected):
        plan_seed({"yf_max_shards": 0}, {})


def test_env_only_anahtar_reddedilir() -> None:
    """Sirlar tohum dosyasina GIREMEZ; dosya depoya girer."""
    with pytest.raises(SettingRejected):
        plan_seed({"yf_proxy_secret_key": "abc"}, {})


def test_adopt_env_yalniz_satirsiz_ve_JSON_disi_anahtarlari_doldurur() -> None:
    env = {"yf_max_shards": "8", "yf_news_tab": "news", "yf_screen_keys": ""}
    plan = plan_seed(
        {"yf_max_shards": 4},
        {"yf_news_tab": "all"},
        adopt_env=env,
    )
    # JSON'daki anahtar JSON degerini alir, env'inkini DEGIL.
    assert plan["yf_max_shards"] == "4"
    # Satiri olan anahtara dokunulmaz.
    assert "yf_news_tab" not in plan
    # Satiri olmayan, JSON disi anahtar env'den devralinir. Bos dize de
    # mesru bir degerdir ve YAZILIR.
    assert plan["yf_screen_keys"] == ""


def test_adopt_env_verilmezse_kapsam_JSON_ile_sinirli() -> None:
    plan = plan_seed({"yf_max_shards": 8}, {})
    assert set(plan) == {"yf_max_shards"}


def test_adopt_env_values_39_anahtari_metin_olarak_verir() -> None:
    values = adopt_env_values()
    assert set(values) == DB_MANAGED_FIELDS
    assert all(isinstance(v, str) for v in values.values())


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, "true"), (False, "false"), (8, "8"), (2.0, "2.0"), ("all", "all"), ("", "")],
)
def test_serilestirme_kurali(value: object, expected: str) -> None:
    """`"True"` YAZILMAZ: pydantic onu da cozerdi ama `.env` bicimiyle
    gidis-donus esitligi bozulurdu (CFG S4.4)."""
    assert serialize(value) == expected
