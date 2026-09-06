"""Aktif proxy kontrolu: ham curl_cffi istegi, yfinance KULLANILMAZ.

yf.config process-global oldugu icin N proxy'yi yfinance uzerinden
paralel kontrol etmek N process gerektirirdi.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from yfin.logging_setup import scrub
from yfin.proxy.dsn import ProxyEndpoint
from yfin.proxy.health import HealthEvent

# Gercek sync trafiginin gectigi iki nokta. Chart crumb istemez
# (data.py:442-447), ama her istek /v1/test/getcrumb'dan da gecer; tek
# endpoint'lik bir kontrol crumb'da bloklanan proxy'yi healthy raporlardi.
CHECK_CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart/AAPL?range=1d&interval=1d"
CHECK_CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"

@dataclass(frozen=True)
class CheckResult:
    label: str
    event: HealthEvent | None  # None = durum degismedi (parola cozulemedi)
    latency_ms: int | None = None
    detail: str = ""


def _new_check_session(endpoint: ProxyEndpoint | None) -> tuple[object, bool]:
    """(session, impersonated). curl_cffi yoksa duz requests'e duser."""
    try:
        from curl_cffi import requests as backend

        session = backend.Session(impersonate="chrome")
        impersonated = True
    except ImportError:  # pragma: no cover - ortama bagli
        import requests as backend  # type: ignore[no-redef]

        session = backend.Session()
        impersonated = False
    if endpoint is not None:
        dsn = endpoint.dsn()
        session.proxies = {"http": dsn, "https": dsn}
    return session, impersonated


def check_endpoint(endpoint: ProxyEndpoint, timeout: float) -> CheckResult:
    """yfinance KULLANILMAZ: yf.config process-global oldugu icin N
    proxy'yi paralel kontrol etmek N process gerektirirdi. Ham istek
    basit bir thread havuzunda kosar.

    SINIR: ham istekte cookie jar bostur, gercek istekler cookie'li
    gider. Bu yuzden `check` TAMAMLAYICIDIR; birincil saglik kaynagi
    pasif gozlemdir (sync sonuclari).
    """
    label = endpoint.host
    session, impersonated = _new_check_session(endpoint)
    detail = "" if impersonated else "curl_cffi yok: TLS taklidi olmadan olculdu"
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
                # Consent/captcha sayfasina yonlendirme: proxy CALISIYOR
                # ama Yahoo onu engelliyor. `<400` hepsini SUCCESS sayardi
                # ve olu bir proxy havuzda saglikli gorunurdu.
                return CheckResult(label, HealthEvent.BLOCKED, latency, f"{url} -> {code}")
            if code != 200:
                # Basari olcutu 200'dur; 2xx'in geri kalani (204, 206...)
                # bu uclarda beklenmez ve dogrulanmamis sayilir.
                return CheckResult(label, HealthEvent.NETWORK, latency, f"{url} -> {code}")
    except Exception as exc:  # noqa: BLE001 - her tasima hatasi NETWORK'tur
        return CheckResult(label, HealthEvent.NETWORK, None, scrub(f"{type(exc).__name__}: {exc}"))
    finally:
        close = getattr(session, "close", None)
        if callable(close):
            close()
    return CheckResult(label, HealthEvent.SUCCESS, latency, detail)
