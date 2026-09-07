"""The in-process per-key minute window, shared by /health/ready and the
UI login endpoint."""

from __future__ import annotations

import pytest

from yfin.api.core.window import FixedWindow


def test_allows_up_to_the_limit_then_refuses() -> None:
    window = FixedWindow()
    assert window.allow("k", 2)
    assert window.allow("k", 2)
    assert not window.allow("k", 2)


def test_keys_are_independent() -> None:
    window = FixedWindow()
    assert window.allow("a", 1)
    assert window.allow("b", 1)
    assert not window.allow("a", 1)


def test_the_window_resets_when_the_minute_rolls(monkeypatch: pytest.MonkeyPatch) -> None:
    import yfin.api.core.window as mod

    now = [1_000_000.0]
    monkeypatch.setattr(mod.time, "time", lambda: now[0])
    window = FixedWindow()
    assert window.allow("k", 1)
    assert not window.allow("k", 1)
    now[0] += 60
    assert window.allow("k", 1)


def test_meta_still_exposes_the_old_names() -> None:
    from yfin.api.routers import meta

    assert meta._FixedWindow is FixedWindow
    assert isinstance(meta._limiter, FixedWindow)
