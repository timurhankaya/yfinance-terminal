"""`settings_schema()` must be pure. No network, no DB: the schema is fixed for the
process's lifetime, while `value` can change on every read."""

from __future__ import annotations

from yfin.core.config import DB_MANAGED_FIELDS, SETTING_GROUPS, Settings, settings_schema


def test_record_count_and_ordering() -> None:
    items = settings_schema()
    assert len(items) == len(DB_MANAGED_FIELDS)
    assert [i.key for i in items] == sorted(DB_MANAGED_FIELDS)


def test_env_only_fields_are_NOT_in_the_schema() -> None:
    """The env-only fields cannot be managed from the panel; if they
    appeared in the schema, the panel would render a form field for them
    and a write attempt would be rejected."""
    keys = {i.key for i in settings_schema()}
    assert not keys & {"db_host", "db_password", "yf_proxy_secret_key", "log_level"}


def test_min_max_Field_kisitlarindan_TURETILIR() -> None:
    """If written by hand, `ge=1` could become `ge=2` one day and the panel
    would validate against a stale range."""
    by_key = {i.key: i for i in settings_schema()}
    assert (by_key["yf_max_shards"].min, by_key["yf_max_shards"].max) == (1.0, None)
    assert (by_key["yf_calendar_page_limit"].min, by_key["yf_calendar_page_limit"].max) == (
        1.0,
        100.0,
    )
    # `gt=0` also counts as min: same informational value for the panel.
    assert by_key["yf_rate_limit_per_sec"].min == 0.0
    assert by_key["yf_news_tab"].min is None


def test_type_default_and_group_come_from_the_model() -> None:
    by_key = {i.key: i for i in settings_schema()}
    for key, item in by_key.items():
        assert item.default == Settings.model_fields[key].default
        assert item.group in SETTING_GROUPS
        assert item.type in {"bool", "int", "float", "str"}


def test_the_same_call_returns_the_same_result() -> None:
    """Purity: since it never looks at the DB, it cannot differ between calls."""
    assert settings_schema() == settings_schema()
