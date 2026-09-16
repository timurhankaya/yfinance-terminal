"""Error classification.

Independent of client.py so the proxy package can use ErrorKind without
importing the yfinance wrapper."""

from __future__ import annotations

import enum
import re
from http import HTTPStatus

from curl_cffi.requests import exceptions as _curl_exc
from yfinance import exceptions as yf_exceptions

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


# Programming/data errors are never retried: they are deterministic, so a
# retry cannot help. Checked before text matching, since a message
# like ValueError("invalid connection string") would otherwise match the
# "connection" marker.
class DatasetOutOfScope(Exception):
    """Dataset is out of scope for this symbol; no network call was made.

    Not a ValueError: _NEVER_RETRYABLE includes ValueError, which would classify
    an out-of-scope symbol as a DATA error and pollute proxy health."""

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
# never penalized.
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


#: Yahoo answering "nothing in that range" for a price request. The message
#: names a cause it does not actually know; the honest reading is an empty
#: result, not a broken fetch.
NO_DATA_EXC = (yf_exceptions.YFPricesMissingError, yf_exceptions.YFInvalidPeriodError)


def is_no_data(exc: BaseException) -> bool:
    return isinstance(exc, NO_DATA_EXC)


def classify_error(exc: BaseException) -> ErrorKind:
    """Map an exception to the class the proxy policy understands.

    Order matters: curl_cffi's RequestException derives from OSError, so the
    HTTP status check runs before any "OSError -> NETWORK" rule."""
    if isinstance(exc, yf_exceptions.YFRateLimitError):
        return ErrorKind.RATE_LIMITED

    code = _status_code(exc)
    if code is not None:
        return _kind_from_status(code)

    # YFPricesMissingError means "no price in this range" (holiday, new IPO,
    # closed exchange), not "invalid symbol", and falls to DATA; otherwise
    # every holiday would look like unknown_symbol.
    if isinstance(exc, yf_exceptions.YFTzMissingError):
        return ErrorKind.UNKNOWN_SYMBOL
    if is_no_data(exc) or isinstance(exc, yf_exceptions.YFDataException):
        return ErrorKind.DATA
    if isinstance(exc, yf_exceptions.YFTickerMissingError):
        return ErrorKind.UNKNOWN_SYMBOL

    if isinstance(exc, _NETWORK_EXC):
        return ErrorKind.NETWORK
    if isinstance(exc, _DATA_EXC):
        return ErrorKind.DATA

    if isinstance(exc, _NEVER_RETRYABLE):
        return ErrorKind.DATA

    if isinstance(exc, TimeoutError | ConnectionError):
        return ErrorKind.NETWORK

    text = f"{type(exc).__name__} {exc}".lower()
    if _HTTP_STATUS_RE.search(text) and _HTTP_CONTEXT_RE.search(text):
        return ErrorKind.RATE_LIMITED if "429" in text else ErrorKind.NETWORK
    if any(marker in text for marker in _BLOCKED_MARKERS):
        return ErrorKind.BLOCKED
    if any(marker in text for marker in _RETRYABLE_MARKERS):
        return ErrorKind.NETWORK
    return ErrorKind.DATA


# BLOCKED is never retried: the health state machine cools the proxy down anyway.
_RETRY_KINDS = frozenset({ErrorKind.RATE_LIMITED, ErrorKind.NETWORK})


def is_retryable(exc: BaseException) -> bool:
    return classify_error(exc) in _RETRY_KINDS


# yfinance reads trailing data with .iloc when it is empty and raises this message.
_ABSENT_INDEX_MESSAGE = "positional indexers are out-of-bounds"


def is_absent_data(exc: BaseException) -> bool:
    """Is this "no such data for this symbol", rather than a real failure?

    `yf.config.debug.hide_exceptions = False` turns yfinance's "404 -> empty
    dict" into an exception; a non-company symbol gets a 404 from Yahoo's
    fundamentals endpoints, and that is `empty`, not `failed`."""
    if _status_code(exc) == HTTPStatus.NOT_FOUND:
        return True
    return isinstance(exc, IndexError) and _ABSENT_INDEX_MESSAGE in str(exc)
