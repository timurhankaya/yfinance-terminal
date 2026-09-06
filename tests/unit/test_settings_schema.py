"""`settings_schema()` SAF olmalidir (CFG S6.4). Ag ve DB YOK.

Sema ile durum bilincli olarak ayrildi: sema surec omru boyunca
sabittir, `value` her okumada degisebilir. Birlesik olsalardi bu dosya
bir DB baglantisi olmadan kosamazdi.
"""

from __future__ import annotations

from yfin.config import DB_MANAGED_FIELDS, SETTING_GROUPS, Settings, settings_schema


def test_kayit_sayisi_ve_siralama() -> None:
    items = settings_schema()
    assert len(items) == len(DB_MANAGED_FIELDS)
    assert [i.key for i in items] == sorted(DB_MANAGED_FIELDS)


def test_env_only_alanlar_semada_YOK() -> None:
    """8 env-only alan panelden yonetilemez; semada gorunselerdi panel
    onlar icin bir form alani cizer ve yazma denemesi reddedilirdi."""
    keys = {i.key for i in settings_schema()}
    assert not keys & {"db_host", "db_password", "yf_proxy_secret_key", "log_level"}


def test_min_max_Field_kisitlarindan_TURETILIR() -> None:
    """Elle yazilsaydi `ge=1` bir gun `ge=2` olur ve panel bayat bir
    araligi dogrulardi."""
    by_key = {i.key: i for i in settings_schema()}
    assert (by_key["yf_max_shards"].min, by_key["yf_max_shards"].max) == (1.0, None)
    assert (by_key["yf_calendar_page_limit"].min, by_key["yf_calendar_page_limit"].max) == (
        1.0,
        100.0,
    )
    # `gt=0` de min sayilir: panel icin bilgi degeri aynidir.
    assert by_key["yf_rate_limit_per_sec"].min == 0.0
    assert by_key["yf_news_tab"].min is None


def test_tip_default_ve_grup_modelden_gelir() -> None:
    by_key = {i.key: i for i in settings_schema()}
    for key, item in by_key.items():
        assert item.default == Settings.model_fields[key].default
        assert item.group in SETTING_GROUPS
        assert item.type in {"bool", "int", "float", "str"}


def test_ayni_cagri_ayni_sonucu_verir() -> None:
    """Saflik: DB'ye bakmadigi icin iki cagri arasinda degisemez."""
    assert settings_schema() == settings_schema()
