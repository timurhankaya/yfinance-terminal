"""An in-process fixed-window counter.

Two byte-identical copies of this existed -- `meta._FixedWindow`, which
keeps a flood off `/health/ready`, and `token_endpoint._ProcessLimiter`,
the fallback that takes over when Redis is gone. They differed only in
that one hard-coded the sixty and the other named it, and only one of
them had `reset()`.

Deliberately tiny, and deliberately NOT the API's rate limiter: that is
`limiter.py`, which is a token bucket in Redis because a fixed window
admits twice the rate across a window boundary. Both users here want a
crude brake on one endpoint, not a fair one.
"""

from __future__ import annotations

import threading
import time


class FixedWindow:
    """Per-key counter, reset when the window rolls.

    The whole dict is dropped on a roll rather than aged per entry: that
    is what keeps a long uptime from growing it without bound, and it is
    why `_window` is compared rather than each entry carrying a timestamp.
    """

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
        """Whether the key has already spent its limit, WITHOUT charging
        the window.

        `allow` asks "may this one through, and count it". A caller that
        counts only SOME of what it sees -- the admin login, which charges
        failures and lets a correct credential through free -- needs the
        two halves apart, or every request it waves through would still
        push the key towards the ceiling.
        """
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
