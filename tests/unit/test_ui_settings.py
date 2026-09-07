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
        ApiSettings(_env_file=None, ui_enabled=True, ui_public=False, ui_password="").validate_ui()


def test_enabled_with_a_password_validates() -> None:
    ApiSettings(_env_file=None, ui_enabled=True, ui_password=PW).validate_ui()


def test_public_mode_needs_no_password() -> None:
    ApiSettings(_env_file=None, ui_enabled=True, ui_public=True, ui_password="").validate_ui()
    assert ApiSettings(_env_file=None).ui_public is True


def test_disabled_never_validates_the_password() -> None:
    ApiSettings(_env_file=None, ui_enabled=False, ui_password="").validate_ui()


def test_cookie_is_secure_only_behind_https() -> None:
    https = ApiSettings(_env_file=None, public_base_url="https://yfin.example")
    http = ApiSettings(_env_file=None, public_base_url="http://localhost:8000")
    none = ApiSettings(_env_file=None, public_base_url="")
    assert https.ui_cookie_secure() is True
    assert http.ui_cookie_secure() is False
    assert none.ui_cookie_secure() is False
