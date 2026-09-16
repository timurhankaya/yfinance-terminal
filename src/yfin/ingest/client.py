"""yfinance wrapper: process-global rate limit + retry."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

import yfinance as yf
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from yfin.core import metrics
from yfin.core.config import Settings, get_settings
from yfin.core.errors import classify_error, is_absent_data, is_retryable
from yfin.core.logging_setup import bridge_yfinance_logging, get_logger

log = get_logger(__name__)


class TokenBucket:
    """Process-global, lock-protected token bucket.

    tenacity retries also go through this, otherwise backoff retries would
    exceed the limit.
    """

    def __init__(self, rate_per_sec: float, burst: float | None = None) -> None:
        self._rate = rate_per_sec
        self._capacity = burst if burst is not None else max(1.0, rate_per_sec)
        self._tokens = self._capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                deficit = 1.0 - self._tokens
                wait = deficit / self._rate
            time.sleep(wait)


_bucket: TokenBucket | None = None
_bucket_lock = threading.Lock()


def get_rate_limiter() -> TokenBucket:
    global _bucket
    with _bucket_lock:
        if _bucket is None:
            _bucket = TokenBucket(get_settings().yf_rate_limit_per_sec)
        return _bucket


def _log_retry(state: RetryCallState) -> None:
    assert state.outcome is not None  # tenacity sleeps only after a failed attempt
    exc = state.outcome.exception()
    assert exc is not None
    metrics.inc("yfin_sync_retries_total", kind=classify_error(exc).value)
    log.debug("yahoo retry", attempt=state.attempt_number, error=str(exc))


def call_optional[T](fn: Callable[[], T], *, what: str) -> T | None:
    """Like call_yahoo, but returns None when data is legitimately absent.

    Used only on endpoints where absence is legitimate (financials,
    calendar, sec_filings). Every other error propagates as-is.
    """
    try:
        return call_yahoo(fn, what=what)
    except Exception as exc:
        if is_absent_data(exc):
            log.debug("no data", what=what, reason=str(exc)[:80])
            return None
        raise


def call_yahoo[T](fn: Callable[[], T], *, what: str) -> T:
    """Calls Yahoo with rate limiting and exponential backoff."""
    settings = get_settings()
    limiter = get_rate_limiter()

    retryer = Retrying(
        stop=stop_after_attempt(settings.yf_retry_attempts),
        wait=wait_exponential_jitter(
            initial=settings.yf_retry_initial_sec, max=settings.yf_retry_max_sec
        ),
        retry=retry_if_exception(is_retryable),
        before_sleep=_log_retry,
        reraise=True,
    )

    def guarded() -> T:
        limiter.acquire()
        return fn()

    log.debug("yahoo call", what=what)
    return retryer(guarded)


def make_ticker(symbol: str) -> yf.Ticker:
    """Fresh Ticker. The curl_cffi session is managed by yfinance 1.7 and
    mimics a browser TLS fingerprint to reduce the block rate."""
    return yf.Ticker(symbol)


# --- yfinance setup ---------------------------------------------------------


def configure_yfinance(
    proxy_dsn: str | None = None,
    *,
    proxy_key: str = "direct",
    settings: Settings | None = None,
) -> None:
    """Called once per process, at shard startup.

    `yf.config` is a process-global singleton, so the process is the unit of
    proxy rotation. `yf.set_config()` is deprecated and only accepts proxy/retries."""
    cfg = settings or get_settings()

    # The bridge attaches BEFORE the debug.logging assignment: that assignment
    # installs yfinance's own unredacted StreamHandler on a handler-less logger.
    bridge_yfinance_logging()

    yf.config.network.proxy = proxy_dsn
    # Disables yfinance's internal exception-retry so it doesn't double up
    # with tenacity's backoff. The cookie-strategy retry at data.py:483-501
    # is independent of this and can't be disabled -- one tenacity attempt
    # is >=2 real Yahoo requests.
    yf.config.network.retries = 0
    # Default True; yfinance swallows internal errors and returns an empty
    # result. Disabling it means cells that looked 'empty' show up as
    # 'failed' instead.
    yf.config.debug.hide_exceptions = False
    yf.config.debug.logging = cfg.log_level.upper() == "DEBUG"

    # The tz + cookie + ISIN cache (name is misleading: it delegates to
    # set_cache_location) is SQLite; N processes writing the same file hit
    # SQLITE_BUSY. The directory is keyed by proxy identity, not shard
    # index: shard-0 may get a different proxy on the next run, and a
    # cookie minted under A's IP would otherwise be used under B's egress IP.
    cache_dir = Path(cfg.yf_tz_cache_dir) / proxy_key
    cache_dir.mkdir(parents=True, exist_ok=True)
    yf.set_tz_cache_location(str(cache_dir))

    # NestedConfig silently returns None for an unknown key (config.py:9);
    # without this check a typo would otherwise go unnoticed.
    if yf.config.debug.hide_exceptions is not False or yf.config.network.retries != 0:
        raise RuntimeError("yfinance config was not applied; check key names")
