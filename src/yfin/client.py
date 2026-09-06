"""yfinance sarmalayicisi: process-global rate limit + retry (S7.5)."""

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

from yfin.config import Settings, get_settings
from yfin.errors import is_absent_data, is_retryable
from yfin.logging_setup import bridge_yfinance_logging, get_logger

log = get_logger(__name__)


class TokenBucket:
    """Process-global ve kilitli token-bucket (S7.1).

    tenacity retry'lari da buradan gecer; aksi halde backoff sirasindaki
    tekrar istekleri limiti asar.
    """

    def __init__(self, rate_per_sec: float, burst: float | None = None) -> None:
        self._rate = rate_per_sec
        self._capacity = burst if burst is not None else max(1.0, rate_per_sec)
        self._tokens = self._capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
                self._last = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
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
    log.warning(
        "yahoo retry",
        attempt=state.attempt_number,
        error=str(state.outcome.exception()) if state.outcome else None,
    )


def call_optional[T](fn: Callable[[], T], *, what: str) -> T | None:
    """call_yahoo, ama "veri yok" durumunda None doner (S8.2).

    Yalnizca yoklugu MESRU olan uclarda kullanilir (financials, calendar,
    sec_filings). Diger her hata oldugu gibi yukselir.
    """
    try:
        return call_yahoo(fn, what=what)
    except Exception as exc:
        if is_absent_data(exc):
            log.info("veri yok", what=what, reason=str(exc)[:80])
            return None
        raise


def call_yahoo[T](fn: Callable[[], T], *, what: str) -> T:
    """Rate limit + exponential backoff ile Yahoo cagrisi."""
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
    """Taze Ticker. curl_cffi oturumu yfinance 1.7 tarafindan yonetilir ve
    tarayici TLS parmak izi taklit ederek engellenme oranini dusurur."""
    return yf.Ticker(symbol)


# --- yfinance kurulumu (P6.1) ---------------------------------------------


def configure_yfinance(
    proxy_dsn: str | None = None,
    *,
    proxy_key: str = "direct",
    settings: Settings | None = None,
) -> None:
    """Process basina BIR KEZ, shard baslangicinda cagrilir.

    `yf.config` process-global bir singleton'dir (thread-local degil), bu
    yuzden bu ayarlar tum process'i kapsar. Rotasyonun ekseni de bu
    nedenle process'tir.

    `yf.set_config()` KULLANILMAZ: 1.7.0'da DeprecationWarning uretir ve
    yalnizca proxy/retries alir.
    """
    cfg = settings or get_settings()

    # SIRA BAGLAYICIDIR: kopru handler'i debug.logging atamasindan ONCE
    # baglanir. Bu atama _enable_debug_mode()'u tetikler ve yfinance,
    # logger'da HIC handler yoksa kendi StreamHandler'ini ekleyip seviyeyi
    # DEBUG'a zorlar - her satir iki kez basilirdi.
    bridge_yfinance_logging()

    yf.config.network.proxy = proxy_dsn
    # yfinance'in ic istisna-retry'ini kapatir; tenacity ile cift backoff
    # olmasin. NOT: data.py:483-501'deki cookie-stratejisi retry'i bundan
    # BAGIMSIZDIR ve kapatilamaz - tenacity'nin bir denemesi >=2 gercek
    # Yahoo istegidir.
    yf.config.network.retries = 0
    # Varsayilan True; yfinance ic hatalari yutup bos sonuc donduruyor.
    # Kapatinca 'empty' sanilan hucreler gercekte 'failed' gorunur.
    yf.config.debug.hide_exceptions = False
    yf.config.debug.logging = cfg.log_level.upper() == "DEBUG"

    # tz + cookie + ISIN cache'i (isim yaniltici: set_cache_location'a
    # delege eder) SQLite'tir; N process ayni dosyaya yazarsa SQLITE_BUSY.
    # Dizin shard_index ile DEGIL proxy kimligiyle anahtarlanir: shard-0
    # bir sonraki run'da baska bir proxy olabilir ve A'nin IP'siyle
    # mintlenmis cookie B'nin cikis IP'siyle kullanilirdi.
    cache_dir = Path(cfg.yf_tz_cache_dir) / proxy_key
    cache_dir.mkdir(parents=True, exist_ok=True)
    yf.set_tz_cache_location(str(cache_dir))

    # NestedConfig bilinmeyen anahtarda SESSIZCE None doner (config.py:9);
    # yazim hatasi aksi halde hic fark edilmezdi.
    if yf.config.debug.hide_exceptions is not False or yf.config.network.retries != 0:
        raise RuntimeError("yfinance config uygulanmadi; anahtar adlarini kontrol edin")
