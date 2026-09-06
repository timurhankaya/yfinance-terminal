"""Schema contract of the `settings` table. Real DB."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text

pytestmark = pytest.mark.repo


def test_setting_key_is_CASE_SENSITIVE(
    test_engine: Engine, clean_settings_table: None
) -> None:
    """The point of case sensitivity is not to prevent collisions (the CLI
    already applies `strip().lower()`): a `YF_MAX_SHARDS` row inserted via
    raw SQL must stay separate and visible from the canonical row, so the
    loader's "unknown key" warning catches it.
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
    assert count == 2, "a case-insensitive collation collapsed two rows into one"


def test_value_NOT_NULL(test_engine: Engine, clean_settings_table: None) -> None:
    """"No override" means the row is absent, not NULL: an empty string is
    a legitimate value (`yf_news_tab=""`), and treating NULL as "no
    override" would make that value unrepresentable."""
    from sqlalchemy.exc import IntegrityError

    with test_engine.connect() as conn, pytest.raises(IntegrityError):
        conn.execute(text("INSERT INTO settings (setting_key, value) VALUES ('yf_news_tab', NULL)"))


def test_an_empty_string_is_a_LEGITIMATE_value(
    test_engine: Engine, clean_settings_table: None
) -> None:
    with test_engine.connect() as conn:
        conn.execute(
            text("INSERT INTO settings (setting_key, value) VALUES ('yf_screen_keys', '')")
        )
        conn.commit()
        value = conn.execute(
            text("SELECT value FROM settings WHERE setting_key = 'yf_screen_keys'")
        ).scalar()
    assert value == ""
