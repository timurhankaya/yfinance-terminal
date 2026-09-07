"""UI settings: the switch, the password and the cookie's Secure flag."""

from __future__ import annotations

import pytest

from yfin.api.core.config import ApiSettings

PW = "hunter2"


def test_ui_is_off_by_default() -> None:
    # _env_file=None: the developer's own .env must not decide this test.
    assert ApiSettings(_env_file=None).ui_enabled is False
    assert ApiSettings(_env_file=None).ui_password == ""


def test_enabled_without_a_password_is_REFUSED() -> None:
    """A UI with an empty password is not "no auth", it is a lie: the login
    form would accept the empty string. Refuse at startup instead."""
    with pytest.raises(ValueError, match="YFAPI_UI_PASSWORD"):
        ApiSettings(ui_enabled=True, ui_password="").validate_ui()


def test_enabled_with_a_password_validates() -> None:
    ApiSettings(ui_enabled=True, ui_password=PW).validate_ui()


def test_disabled_never_validates_the_password() -> None:
    ApiSettings(ui_enabled=False, ui_password="").validate_ui()


def test_cookie_is_secure_only_behind_https() -> None:
    assert ApiSettings(public_base_url="https://yfin.example").ui_cookie_secure() is True
    assert ApiSettings(public_base_url="http://localhost:8000").ui_cookie_secure() is False
    assert ApiSettings(public_base_url="").ui_cookie_secure() is False
