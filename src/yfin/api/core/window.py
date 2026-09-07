"""A per-key counter in a one-minute window, in process.

Deliberately tiny and deliberately not Redis: it protects single
endpoints from floods (readiness probes, the UI login form), it is not
the API's rate limiter. Under `uvicorn --workers N` each process keeps
its own counts, so the effective limit is N times the configured one.
Both call sites accept that.
"""

from __future__ import annotations

import threading
import time


class FixedWindow:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._window = 0
        self._hits: dict[str, int] = {}

    def allow(self, key: str, limit: int) -> bool:
        window = int(time.time() // 60)
        with self._lock:
            if window != self._window:
                self._window = window
                self._hits = {}
            count = self._hits.get(key, 0) + 1
            self._hits[key] = count
            return count <= limit
