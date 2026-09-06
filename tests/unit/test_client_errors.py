"""classify_error vaka tablosu (P5.2).

En kritik kural SIRADIR: curl_cffi'nin RequestException'i OSError
TUREVIDIR (HTTPError -> RequestException -> CurlError -> OSError), bu
yuzden bir "OSError -> NETWORK" kurali 403'leri de NETWORK sanar ve
proxy'yi HAKSIZ cezalandirirdi.
"""

from __future__ import annotations

from typing import Any

import pytest
from curl_cffi.requests import exceptions as curl_exc
from yfinance import exceptions as yf_exc

from yfin.core.errors import ErrorKind, classify_error, is_retryable


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _http_error(status: int) -> Exception:
    exc = curl_exc.HTTPError(f"{status} Error")
    exc.response = _Response(status)  # type: ignore[attr-defined]
    return exc


class TestStatusCodes:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (429, ErrorKind.RATE_LIMITED),
            (401, ErrorKind.BLOCKED),
            (403, ErrorKind.BLOCKED),
            (500, ErrorKind.NETWORK),
            (503, ErrorKind.NETWORK),
            (404, ErrorKind.DATA),
            (422, ErrorKind.DATA),
        ],
    )
    def test_http_status_wins_over_oserror(self, status: int, expected: ErrorKind) -> None:
        assert issubclass(curl_exc.HTTPError, OSError), "on kosul: HTTPError OSError turevi"
        assert classify_error(_http_error(status)) is expected


class TestNonHttpResponse:
    """curl_cffi BAGLANTI hatalarina da bir Response ilistirir ve
    status_code'u 0'dir. Esik olmadan 0 'gecerli yanit' sayilir, DATA'ya
    duser ve OLU BIR PROXY HIC CEZALANDIRILMAZ (canli kosuda gorulen bug).
    """

    def test_zero_status_is_not_a_real_status(self) -> None:
        exc = curl_exc.ConnectionError("Failed to perform, curl: (7) Failed to connect")
        exc.response = _Response(0)  # type: ignore[attr-defined]
        assert classify_error(exc) is ErrorKind.NETWORK
        assert is_retryable(exc)

    def test_proxy_error_with_zero_status_is_network(self) -> None:
        exc = curl_exc.ProxyError("proxy refused")
        exc.response = _Response(0)  # type: ignore[attr-defined]
        assert classify_error(exc) is ErrorKind.NETWORK


class TestTypedExceptions:
    def test_rate_limit_is_typed(self) -> None:
        assert classify_error(yf_exc.YFRateLimitError()) is ErrorKind.RATE_LIMITED

    def test_tz_missing_is_unknown_symbol(self) -> None:
        assert classify_error(yf_exc.YFTzMissingError("XXX")) is ErrorKind.UNKNOWN_SYMBOL

    def test_prices_missing_is_data_not_unknown_symbol(self) -> None:
        """'Bu aralikta fiyat yok' (tatil, yeni IPO) sembolun gecersiz
        oldugu anlamina GELMEZ; aksi halde her tatil gunu unknown_symbol
        olurdu (P6.2)."""
        exc = yf_exc.YFPricesMissingError("AAPL", "no data")
        assert classify_error(exc) is ErrorKind.DATA

    @pytest.mark.parametrize(
        "exc",
        [
            curl_exc.ProxyError("proxy down"),
            curl_exc.DNSError("no such host"),
            curl_exc.ConnectTimeout("timeout"),
            curl_exc.SSLError("handshake"),
            TimeoutError("slow"),
            ConnectionError("reset"),
        ],
    )
    def test_transport_faults_are_network(self, exc: BaseException) -> None:
        assert classify_error(exc) is ErrorKind.NETWORK


class TestDeterministicErrors:
    @pytest.mark.parametrize(
        "exc",
        [
            KeyError("missing"),
            TypeError("bad type"),
            # "connection" kelimesi metin markerina takilirdi; bu yuzden
            # deterministik hatalar metin eslesmesinden ONCE elenir.
            ValueError("invalid connection string: timeout"),
        ],
    )
    def test_programming_errors_are_data_and_never_retried(self, exc: BaseException) -> None:
        assert classify_error(exc) is ErrorKind.DATA
        assert not is_retryable(exc)


class TestTextFallback:
    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("429 Client Error: Too Many Requests for url", ErrorKind.RATE_LIMITED),
            ("503 Server Error: Service Unavailable", ErrorKind.NETWORK),
            ("HTTP 502 Bad Gateway", ErrorKind.NETWORK),
        ],
    )
    def test_status_needs_http_context(self, message: str, expected: ErrorKind) -> None:
        assert classify_error(RuntimeError(message)) is expected

    def test_bare_number_is_not_a_status_code(self) -> None:
        """'Symbol 500 not found' bir 5xx yaniti DEGILDIR."""
        assert classify_error(RuntimeError("Symbol 500 not found")) is ErrorKind.DATA


class TestRetryPolicy:
    def test_blocked_is_not_retried(self) -> None:
        """Banlanmis proxy'de 5 deneme ~30 sn ve token-bucket bosa gider;
        saglik durum makinesi zaten cooldown'a alacaktir (P5.2)."""
        exc = _http_error(403)
        assert classify_error(exc) is ErrorKind.BLOCKED
        assert not is_retryable(exc)

    @pytest.mark.parametrize("exc", [yf_exc.YFRateLimitError(), curl_exc.ConnectTimeout("t")])
    def test_rate_limited_and_network_are_retried(self, exc: BaseException) -> None:
        assert is_retryable(exc)

    def test_unknown_symbol_is_not_retried(self) -> None:
        assert not is_retryable(yf_exc.YFTzMissingError("XXX"))


class TestRedaction:
    def test_scrub_hides_password_in_dsn(self) -> None:
        from yfin.core.logging_setup import scrub

        assert scrub("socks5h://acct:s3cret@10.0.0.1:1080") == "socks5h://acct:***@10.0.0.1:1080"

    def test_scrub_leaves_plain_urls_untouched(self) -> None:
        from yfin.core.logging_setup import scrub

        url = "https://query2.finance.yahoo.com/v8/finance/chart/AAPL"
        assert scrub(url) == url

    def test_processor_scrubs_event_fields(self) -> None:
        from yfin.core.logging_setup import redact_credentials

        event: dict[str, Any] = {"proxy": "http://acct:s3cret@10.0.0.1:3128", "n": 1}
        assert redact_credentials(None, "info", event)["proxy"] == "http://acct:***@10.0.0.1:3128"
