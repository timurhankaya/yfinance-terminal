"""The test infrastructure itself: per-process schema.

If this logic silently breaks, two concurrent pytest runs will again drop
each other's tables, and the bug will be looked for in the CODE; that is
why this is tested.
"""

from __future__ import annotations

import os

from helpers import pid_is_alive, schema_name


def test_schema_name_is_process_scoped() -> None:
    name = schema_name("yfinance_test")
    assert name == f"yfinance_test_{os.getpid()}"
    assert name != "yfinance_test"  # the base schema is NEVER used


def test_current_process_is_alive() -> None:
    assert pid_is_alive(os.getpid())


def test_unused_pid_is_not_alive() -> None:
    """Cleanup only drops the schema of DEAD PIDs."""
    assert not pid_is_alive(999_999)
