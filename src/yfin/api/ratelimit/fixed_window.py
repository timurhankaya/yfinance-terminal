"""An in-process fixed-window counter: a crude brake on one endpoint, not
the API's rate limiter (`limiter.py`, a token bucket in Redis, because a
fixed window admits twice the rate across a boundary)."""

from __future__ import annotations

import threading
import time


class FixedWindow:
    """Per-key counter, reset when the window rolls. The whole dict is dropped
    on a roll rather than aged per entry, so a long uptime cannot grow it
    without bound."""

    def __init__(self, window_seconds: int = 60) -> None:
        self._window_seconds = window_seconds
        self._lock = threading.Lock()
        self._window = 0
        self._hits: dict[str, int] = {}

    def allow(self, key: str, limit: int) -> bool:
        window = int(time.time() // self._window_seconds)
        with self._lock:
            if window != self._window:
                self._window = window
                self._hits = {}
            count = self._hits.get(key, 0) + 1
            self._hits[key] = count
            return count <= limit

    def over(self, key: str, limit: int) -> bool:
        """Whether the key has already spent its limit, WITHOUT charging the
        window. A caller that counts only some of what it sees (the admin
        login charges failures only) needs this apart from `allow`."""
        window = int(time.time() // self._window_seconds)
        with self._lock:
            # A rolled window is empty, and saying so is enough: the drop
            # belongs to `allow`, so that reading the brake never has a
            # side effect.
            if window != self._window:
                return False
            return self._hits.get(key, 0) >= limit

    def reset(self) -> None:
        """Drops the window. For tests, which share one process."""
        with self._lock:
            self._window = 0
            self._hits = {}
