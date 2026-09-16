"""Active proxy check: raw curl_cffi request, not yfinance.

yf.config is process-global, so checking N proxies in parallel through
yfinance would need N processes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from yfin.core.logging_setup import scrub
from yfin.proxy.dsn import ProxyEndpoint
from yfin.proxy.health import HealthEvent

# The two endpoints real sync traffic hits. Chart doesn't need a crumb
# (data.py:442-447), but every request also goes through
# /v1/test/getcrumb; checking chart alone would report a proxy blocked
# on crumb as healthy.
CHECK_CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart/AAPL?range=1d&interval=1d"
CHECK_CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"

@dataclass(frozen=True)
class CheckResult:
    label: str
    event: HealthEvent | None  # None = no state change (password undecryptable)
    latency_ms: int | None = None
    detail: str = ""


def _new_check_session(endpoint: ProxyEndpoint | None) -> tuple[object, bool]:
    """(session, impersonated). Falls back to plain requests if curl_cffi is absent."""
    try:
        from curl_cffi import requests as backend

        session = backend.Session(impersonate="chrome")
        impersonated = True
    except ImportError:  # pragma: no cover - depends on environment
        import requests as backend  # type: ignore[no-redef]

        session = backend.Session()
        impersonated = False
    if endpoint is not None:
        dsn = endpoint.dsn()
        session.proxies = {"http": dsn, "https": dsn}
    return session, impersonated


def check_endpoint(endpoint: ProxyEndpoint, timeout: float) -> CheckResult:
    """Raw request, not yfinance: yf.config is process-global, so checking N
    proxies in parallel through it would need N processes.

    The raw request has an empty cookie jar, unlike real traffic, so this is
    complementary to passive observation of sync results."""
    label = endpoint.host
    session, impersonated = _new_check_session(endpoint)
    detail = "" if impersonated else "curl_cffi absent: measured without TLS impersonation"
    latency: int | None = None
    try:
        for index, url in enumerate((CHECK_CHART_URL, CHECK_CRUMB_URL)):
            started = time.perf_counter()
            response = session.get(url, timeout=timeout)  # type: ignore[attr-defined]
            if index == 0:
                latency = int((time.perf_counter() - started) * 1000)
            code = int(response.status_code)
            if code == 429:
                return CheckResult(label, HealthEvent.RATE_LIMITED, latency, f"{url} -> 429")
            if code in (401, 403):
                return CheckResult(label, HealthEvent.BLOCKED, latency, f"{url} -> {code}")
            if code >= 500:
                return CheckResult(label, HealthEvent.NETWORK, latency, f"{url} -> {code}")
            if code >= 400:
                return CheckResult(label, HealthEvent.NETWORK, latency, f"{url} -> {code}")
            if 300 <= code < 400:
                # Redirect to a consent/captcha page: the proxy works but
                # Yahoo blocks it. Treating all `<400` as SUCCESS would
                # report a dead proxy as healthy.
                return CheckResult(label, HealthEvent.BLOCKED, latency, f"{url} -> {code}")
            if code != 200:
                # Only 200 counts as success; other 2xx codes (204, 206...)
                # are unexpected here and treated as unverified.
                return CheckResult(label, HealthEvent.NETWORK, latency, f"{url} -> {code}")
    except Exception as exc:  # noqa: BLE001 - every transport error is NETWORK
        return CheckResult(label, HealthEvent.NETWORK, None, scrub(f"{type(exc).__name__}: {exc}"))
    finally:
        close = getattr(session, "close", None)
        if callable(close):
            close()
    return CheckResult(label, HealthEvent.SUCCESS, latency, detail)
