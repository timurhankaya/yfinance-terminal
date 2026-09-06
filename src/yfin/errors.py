"""Error classification.

Separate from and not dependent on client.py, so the proxy domain model
(proxy package) can use ErrorKind without importing the yfinance wrapper;
the dependency arrow points the right way.
"""

from __future__ import annotations

import enum
import re
from http import HTTPStatus

from yfinance import exceptions as yf_exceptions

# curl_cffi is yfinance's preferred backend but not required (_http.py falls
# back to plain requests via YF_DISABLE_CURL_CFFI). Typed classification is
# used when available, text matching otherwise.
try:  # pragma: no cover - environment dependent
    from curl_cffi.requests import exceptions as _curl_exc

    _NETWORK_EXC: tuple[type[BaseException], ...] = (
        _curl_exc.ProxyError,
        _curl_exc.InvalidProxyURL,
        _curl_exc.DNSError,
        _curl_exc.ConnectionError,
        _curl_exc.Timeout,
        _curl_exc.ConnectTimeout,
        _curl_exc.ReadTimeout,
        _curl_exc.SSLError,
        _curl_exc.CertificateVerifyError,
        _curl_exc.ChunkedEncodingError,
    )
    _DATA_EXC: tuple[type[BaseException], ...] = (
        _curl_exc.InvalidJSONError,
        _curl_exc.JSONDecodeError,
        _curl_exc.ContentDecodingError,
    )
except ImportError:  # pragma: no cover
    _NETWORK_EXC = ()
    _DATA_EXC = ()


# "Is this retryable" is not the same as "is this the proxy's fault": an
# invalid symbol or parse error must not send a proxy into cooldown.


class ErrorKind(enum.StrEnum):
    RATE_LIMITED = "rate_limited"
    BLOCKED = "blocked"
    NETWORK = "network"
    DATA = "data"
    UNKNOWN_SYMBOL = "unknown_symbol"


# Only these kinds affect proxy health.
PROXY_FAULT_KINDS = frozenset({ErrorKind.RATE_LIMITED, ErrorKind.BLOCKED, ErrorKind.NETWORK})


# Programming/data errors are never retried: they are deterministic, so 5
# attempts just waste ~30s. Checked before text matching, since a message
# like ValueError("invalid connection string") would otherwise match the
# "connection" marker.
class DatasetOutOfScope(Exception):
    """Dataset is out of scope for this symbol; no network call was made.

    Does not subclass ValueError: _NEVER_RETRYABLE includes ValueError, and
    that base would silently classify an out-of-scope symbol as a DATA
    error and pollute proxy health accounting. `_worker` already catches
    this before the generic `except`, so it never reaches classify_error.
    """

    def __init__(self, interval: str) -> None:
        super().__init__(f"out of scope: {interval}")
        self.interval = interval


_NEVER_RETRYABLE: tuple[type[BaseException], ...] = (
    KeyError,
    IndexError,
    AttributeError,
    TypeError,
    ValueError,
    ZeroDivisionError,
    NotImplementedError,
)

# Text matching is a last resort and kept narrow. A status code only counts
# as retryable alongside HTTP context, so a bare number in a message like
# "Symbol 500 not found" doesn't false-match.
_HTTP_STATUS_RE = re.compile(r"\b(?:429|5\d\d)\b")
_HTTP_CONTEXT_RE = re.compile(r"\b(?:http|https|status|error|client|server|url)\b")

_RETRYABLE_MARKERS = (
    "too many requests",
    "rate limit",
    "server error",
    "service unavailable",
    "gateway",
    "timed out",
    "timeout",
    "connection reset",
    "connection aborted",
    "connection refused",
    "temporarily unavailable",
)

_BLOCKED_MARKERS = ("forbidden", "consent", "captcha", "unauthorized")


# Smallest valid HTTP status. curl_cffi attaches a Response to connection
# errors too, with status_code 0; without this floor, 0 counts as "a valid
# response", falls through to DATA in _kind_from_status, and a dead proxy is
# never penalized (confirmed in a live run).
_MIN_HTTP_STATUS = 100


def _status_code(exc: BaseException) -> int | None:
    """Real HTTP status code carried by the exception, if any."""
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int) and code >= _MIN_HTTP_STATUS:
        return code
    return None


def _kind_from_status(code: int) -> ErrorKind:
    if code == 429:
        return ErrorKind.RATE_LIMITED
    if code in (401, 403):
        return ErrorKind.BLOCKED
    if code >= 500:
        return ErrorKind.NETWORK
    return ErrorKind.DATA


def classify_error(exc: BaseException) -> ErrorKind:
    """Map an exception to the class the proxy policy understands.

    Order matters: curl_cffi's RequestException derives from OSError
    (HTTPError -> RequestException -> CurlError -> OSError), so a plain
    "OSError -> NETWORK" rule would treat 403s as NETWORK and unfairly
    penalize the proxy. The HTTP status check runs first.
    """
    # 1) yfinance's dedicated rate-limit exception
    if isinstance(exc, yf_exceptions.YFRateLimitError):
        return ErrorKind.RATE_LIMITED

    # 2) HTTP status code (before the OSError check)
    code = _status_code(exc)
    if code is not None:
        return _kind_from_status(code)

    # 3) yfinance types. YFPricesMissingError means "no price in this
    #    range" (holiday, new IPO, closed exchange), not "invalid symbol",
    #    and falls to DATA; otherwise every holiday would look like
    #    unknown_symbol.
    if isinstance(exc, yf_exceptions.YFTzMissingError):
        return ErrorKind.UNKNOWN_SYMBOL
    if isinstance(
        exc,
        yf_exceptions.YFPricesMissingError
        | yf_exceptions.YFInvalidPeriodError
        | yf_exceptions.YFDataException,
    ):
        return ErrorKind.DATA
    if isinstance(exc, yf_exceptions.YFTickerMissingError):
        return ErrorKind.UNKNOWN_SYMBOL

    # 4) Transport layer (curl_cffi types)
    if _NETWORK_EXC and isinstance(exc, _NETWORK_EXC):
        return ErrorKind.NETWORK
    if _DATA_EXC and isinstance(exc, _DATA_EXC):
        return ErrorKind.DATA

    # 5) Deterministic programming/data errors
    if isinstance(exc, _NEVER_RETRYABLE):
        return ErrorKind.DATA

    # 6) Built-in network exceptions
    if isinstance(exc, TimeoutError | ConnectionError):
        return ErrorKind.NETWORK

    # 7) Text fallback
    text = f"{type(exc).__name__} {exc}".lower()
    if _HTTP_STATUS_RE.search(text) and _HTTP_CONTEXT_RE.search(text):
        return ErrorKind.RATE_LIMITED if "429" in text else ErrorKind.NETWORK
    if any(marker in text for marker in _BLOCKED_MARKERS):
        return ErrorKind.BLOCKED
    if any(marker in text for marker in _RETRYABLE_MARKERS):
        return ErrorKind.NETWORK
    return ErrorKind.DATA


# BLOCKED is never retried: 5 attempts with jittered backoff on a banned
# proxy burns ~30s and the token bucket for nothing, and the health state
# machine will cooldown that proxy anyway.
_RETRY_KINDS = frozenset({ErrorKind.RATE_LIMITED, ErrorKind.NETWORK})


def is_retryable(exc: BaseException) -> bool:
    return classify_error(exc) in _RETRY_KINDS


# yfinance reads trailing data with .iloc when it is empty and raises this message.
_ABSENT_INDEX_MESSAGE = "positional indexers are out-of-bounds"


def is_absent_data(exc: BaseException) -> bool:
    """Is this "no such data for this symbol", rather than a real failure?

    The project sets `yf.config.debug.hide_exceptions = False` so real
    errors aren't swallowed, but this also turns yfinance's "404 -> empty
    dict" behavior into an exception. A symbol that isn't a company (ETF,
    fund, crypto) gets a 404 from Yahoo's fundamentals endpoints:
    `{"error":{"code":"Not Found","description":"No fundamentals data found
    for symbol: SPY"}}`. That is `empty`, not `failed`.
    """
    if _status_code(exc) == HTTPStatus.NOT_FOUND:
        return True
    return isinstance(exc, IndexError) and _ABSENT_INDEX_MESSAGE in str(exc)
