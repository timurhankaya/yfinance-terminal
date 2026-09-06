"""`settings_state()` ve `export_values()` SAF fonksiyonlardir. DB YOK.

Ikisi de bir zamanlar DB'ye bagliydi (`settings_state` kullanmadigi bir
`Settings` aliyordu, `export_values` CLI komutunun icinde gomuluydu) ve
bu yuzden yalnizca repo testinde -- gercek bir veritabaniyla --
sinanabiliyorlardi. Ayrildiklarinda bu dosya mumkun oldu.
"""

from __future__ import annotations

import pytest

from yfin.config import DB_MANAGED_FIELDS, ENV_ONLY_FIELDS
from yfin.settings_store import (
    KeyVerdict,
    Source,
    classify_key,
    export_values,
    settings_state,
)


def test_rows_None_DB_YE_BAKILMADI_demektir() -> None:
    """Erisilemeyen DB ile `YF_SETTINGS_SOURCE=env` ayni sonuca varir:
    hicbir satir ETKIN DEGILDIR ve kaynak env/default olur."""
    states = settings_state(rows=None)
    assert set(states) == DB_MANAGED_FIELDS
    assert all(not s.has_row for s in states.values())
    assert all(s.source is not Source.DB for s in states.values())


def test_satir_kaynagi_db_yapar() -> None:
    states = settings_state(rows={"yf_max_shards": "9"})
    assert states["yf_max_shards"].value == "9"
    assert states["yf_max_shards"].source is Source.DB
    assert states["yf_max_shards"].has_row is True


def test_has_row_ile_source_AYNI_SEY_DEGILDIR() -> None:
    """Env-only bir satir TABLODA VARDIR ama UYGULANMAZ. Tek bayrakla
    temsil edilseydi bu durum gorunmez olurdu."""
    states = settings_state(rows={"yf_max_shards": "9"})
    assert "db_host" not in states, "env-only alan durum tablosunda yer almaz"


def test_env_de_kurulu_alan_kaynagi_ENV() -> None:
    """Ayrim `model_fields_set` ile yapilir, "deger varsayilandan farkli
    mi" karsilastirmasiyla DEGIL: `.env`de varsayilanla AYNI degeri yazan
    bir kurulum aksi halde `default` gorunurdu."""
    from yfin.config import bootstrap_settings

    env_set = bootstrap_settings().model_fields_set & DB_MANAGED_FIELDS
    if not env_set:  # pragma: no cover - .env'e bagli
        pytest.skip(".env hicbir DB-yonetimli alani kurmuyor")
    states = settings_state(rows={})
    assert all(states[key].source is Source.ENV for key in env_set)


def test_export_varsayilan_yalniz_satiri_olanlari_verir() -> None:
    states = settings_state(rows={"yf_max_shards": "9"})
    assert export_values(states) == {"yf_max_shards": 9}


def test_export_all_39_anahtari_NATIVE_tiple_verir() -> None:
    """Metin donseydi `seed(export(state))` bir sonraki turda `"8"`i
    yeniden serilestirir ve tohum dosyasi tipsiz metne kayardi."""
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
def test_classify_bilinmeyen_anahtarlar(key: str) -> None:
    """Kanonik olmayan bicim (`YF_MAX_SHARDS`) da BILINMEYENDIR: tabloya
    ham SQL ile sokulmus boyle bir satirin GORUNUR kalmasi, duyarli
    collation kararinin gerekcesidir (CFG S2)."""
    assert classify_key(key) is KeyVerdict.UNKNOWN


def test_okuma_ve_yazma_yollari_AYNI_karari_verir() -> None:
    """Politikanin tek kaynaktan geldiginin citi.

    Iki yerde kodlanmis olsaydi, okuma yolunda unutulan bir kategori
    GUVENLIK SINIRINI delerdi (`db_host` iceren bir satirin uygulanmasi).
    """
    from yfin.settings_store import SettingRejected, filter_overrides, validate_pair

    for key in sorted(ENV_ONLY_FIELDS | {"hicboyle_yok"}):
        assert filter_overrides({key: "x"}) == {}, f"{key} okuma yolunda gecti"
        with pytest.raises(SettingRejected):
            validate_pair(key, "x", overrides={})
