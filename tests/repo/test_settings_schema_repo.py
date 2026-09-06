"""`settings` tablosunun sema sozlesmesi. Gercek DB."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text

pytestmark = pytest.mark.repo


def test_setting_key_buyuk_kucuk_harf_DUYARLI(
    test_engine: Engine, clean_settings_table: None
) -> None:
    """Duyarliligin amaci cakismayi onlemek DEGILDIR (CLI zaten
    `strip().lower()` uyguluyor): ham SQL ile sokulmus `YF_MAX_SHARDS`
    satirinin kanonik satirdan AYRI ve GORUNUR kalip yukleyicinin
    "bilinmeyen anahtar" uyarisina takilmasidir (CFG S2/S4.1).
    """
    with test_engine.connect() as conn:
        conn.execute(
            text("INSERT INTO settings (setting_key, value) VALUES ('yf_max_shards', '8')")
        )
        conn.execute(
            text("INSERT INTO settings (setting_key, value) VALUES ('YF_MAX_SHARDS', '9')")
        )
        conn.commit()
        count = conn.execute(text("SELECT count(*) FROM settings")).scalar()
    assert count == 2, "duyarsiz collation iki satiri tek satira indirdi"


def test_value_NOT_NULL(test_engine: Engine, clean_settings_table: None) -> None:
    """"Ezme yok" demek satirin OLMAMASIDIR, NULL degil: bos dize mesru
    bir degerdir (`yf_news_tab=""`) ve NULL'u "ezme yok" saymak o degeri
    TEMSIL EDILEMEZ kilardi (CFG S2)."""
    from sqlalchemy.exc import IntegrityError

    with test_engine.connect() as conn, pytest.raises(IntegrityError):
        conn.execute(text("INSERT INTO settings (setting_key, value) VALUES ('yf_news_tab', NULL)"))


def test_bos_dize_MESRU_bir_degerdir(test_engine: Engine, clean_settings_table: None) -> None:
    with test_engine.connect() as conn:
        conn.execute(
            text("INSERT INTO settings (setting_key, value) VALUES ('yf_screen_keys', '')")
        )
        conn.commit()
        value = conn.execute(
            text("SELECT value FROM settings WHERE setting_key = 'yf_screen_keys'")
        ).scalar()
    assert value == ""
