"""UI settings: the switch and the per-address brake."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from yfin.api.core.config import ApiSettings


def test_ui_is_off_by_default() -> None:
    # _env_file=None: the developer's own .env must not decide this test.
    assert ApiSettings(_env_file=None).ui_enabled is False


def test_the_brake_defaults_to_600_a_minute() -> None:
    assert ApiSettings(_env_file=None).ui_requests_per_minute == 600


def test_a_brake_of_zero_is_REFUSED() -> None:
    """`ge=1`, because the terminal is public and the brake is the only
    thing in front of it: a zero would read as "no limit" and mean
    "no requests at all"."""
    with pytest.raises(ValidationError):
        ApiSettings(_env_file=None, ui_requests_per_minute=0)
