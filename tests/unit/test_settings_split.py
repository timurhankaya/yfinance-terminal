"""Alan bolunmesinin DORT CITI (CFG S8.1).

`DB_MANAGED_FIELDS` elle yazilmis bir liste degil, `ENV_ONLY_FIELDS`in
TAMLAYANIDIR. Bu, yeni bir alanin sessizce hicbir kumeye girmemesini
imkansiz kilar -- ama karsiliginda FAIL-OPEN olur: yeni alan varsayilan
olarak DB'ye acilir. Buradaki dort cit tam olarak o acikligi kapatir.
"""

from __future__ import annotations

import re

import pytest

from yfin.config import (
    DB_MANAGED_FIELDS,
    ENV_ONLY_FIELDS,
    SETTING_GROUPS,
    Settings,
)

# Sayilar 2026-09-06 anlik goruntusudur (spec 47/39 diyordu;
# `yf_probe_sustainability` YAGNI geregi kaldirildi) ve MEKANIZMA bunlara dayanmaz;
# yine de bir alan eklendiginde bu dosyanin okunmasini zorlamak icin
# tutulurlar.
SNAPSHOT_TOTAL = 46
SNAPSHOT_ENV_ONLY = 8
SNAPSHOT_DB_MANAGED = 38

SECRET_NAME_RE = re.compile(r"secret|password|token|credential")


def test_kumeler_tuketicidir_ve_kesismez() -> None:
    """CIT 1 -- tuketicilik.

    Yeni bir `Settings` alani eklendiginde HER ZAMAN iki kumeden birine
    girer; "hicbirine girmeyen" bir alan olamaz.
    """
    assert set(Settings.model_fields) == ENV_ONLY_FIELDS | DB_MANAGED_FIELDS
    assert not (ENV_ONLY_FIELDS & DB_MANAGED_FIELDS)


def test_anlik_goruntu_sayilari() -> None:
    assert len(Settings.model_fields) == SNAPSHOT_TOTAL
    assert len(ENV_ONLY_FIELDS) == SNAPSHOT_ENV_ONLY
    assert len(DB_MANAGED_FIELDS) == SNAPSHOT_DB_MANAGED


@pytest.mark.parametrize("key", sorted(DB_MANAGED_FIELDS))
def test_metadata_eksiksiz(key: str) -> None:
    """CIT 2 -- metadata.

    Panel formu `description` + `group` olmadan cizilemez; eksik birakilan
    bir alan panelde adsiz ve gruplanmamis gorunurdu.
    """
    info = Settings.model_fields[key]
    assert info.description, f"{key}: description yok"
    extra = info.json_schema_extra
    assert isinstance(extra, dict), f"{key}: json_schema_extra yok"
    assert extra.get("group") in SETTING_GROUPS, f"{key}: gecersiz grup {extra.get('group')!r}"


def test_gruplar_hepsi_kullaniliyor() -> None:
    """Kullanilmayan bir grup adi paneli bos bir sekmeyle birakirdi."""
    used = {
        Settings.model_fields[key].json_schema_extra["group"]  # type: ignore[index]
        for key in DB_MANAGED_FIELDS
    }
    assert used == set(SETTING_GROUPS)


@pytest.mark.parametrize("key", sorted(Settings.model_fields))
def test_sir_adi_citi(key: str) -> None:
    """CIT 3 -- SIR ADI kalibi.

    Adi `secret|password|token|credential` kalibina uyan bir alan
    `ENV_ONLY_FIELDS`te DEGILSE tamlayan turetme onu DB'ye acardi ve
    sifreyi, sifreledigi verinin yanina koyardi.
    """
    if SECRET_NAME_RE.search(key):
        assert key in ENV_ONLY_FIELDS, f"{key} sir gibi adlandirilmis ama DB-yonetimli"


@pytest.mark.parametrize("key", sorted(DB_MANAGED_FIELDS))
def test_skaler_citi(key: str) -> None:
    """CIT 4 -- skalerlik.

    `value` sutunu METINDIR ve serilestirme kurali (CFG S4.4) yalnizca
    skalerler icin tanimlidir. Bir alan listeye/sozluge donerse bu test
    patlar ve kurali guncellemeyi ZORUNLU kilar.
    """
    assert Settings.model_fields[key].annotation in (bool, int, float, str)
