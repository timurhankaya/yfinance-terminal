"""Hata siniflandirmasi (P5.2).

Bu modul yfinance sarmalayicisindan (client.py) AYRIDIR ve ona bagimli
DEGILDIR. Boylece proxy alan modeli (proxy paketi) ErrorKind'i, yfinance
sarmalayicisini import etmeden kullanabilir; bagimlilik oku dogru yone
bakar.
"""

from __future__ import annotations

import enum
import re
from http import HTTPStatus

from yfinance import exceptions as yf_exceptions

# curl_cffi yfinance'in tercih ettigi backend'dir ama zorunlu degildir
# (_http.py YF_DISABLE_CURL_CFFI ile duz requests'e duser). Tipli
# siniflandirma varsa kullanilir, yoksa metin geri dususu devreye girer.
try:  # pragma: no cover - ortama bagli
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


# --- hata siniflandirmasi (P5.2) ------------------------------------------
#
# "Retry edilebilir mi" sorusu proxy sagligi icin YETMEZ: her hata
# proxy'nin sucu degildir. Gecersiz sembol veya parse hatasi proxy'yi
# cooldown'a atmamalidir.


class ErrorKind(enum.StrEnum):
    RATE_LIMITED = "rate_limited"
    BLOCKED = "blocked"
    NETWORK = "network"
    DATA = "data"
    UNKNOWN_SYMBOL = "unknown_symbol"


# Proxy sagligini YALNIZ bunlar etkiler
PROXY_FAULT_KINDS = frozenset({ErrorKind.RATE_LIMITED, ErrorKind.BLOCKED, ErrorKind.NETWORK})

# Programlama/veri hatalari ASLA tekrarlanmaz. Bunlar deterministiktir;
# 5 kez denemek yalnizca ~30 sn kaybettirir. Metin eslesmesinden ONCE
# elenirler: ValueError("invalid connection string") gibi bir mesaj aksi
# halde "connection" markerina takilirdi.
class DatasetOutOfScope(Exception):
    """Dataset bu sembol icin kapsam disi; AG CAGRISI YAPILMADI (PB S6.5).

    ValueError'DAN TUREMEZ: _NEVER_RETRYABLE ValueError'i iceriyor ve
    boyle bir taban, kapsam disiligi sessizce bir VERI HATASI olarak
    siniflandirip proxy saglik muhasebesini kirletirdi. Zaten
    `_worker` bunu jenerik `except`ten ONCE yakalar ve
    classify_error'a hic ugramaz.
    """

    def __init__(self, interval: str) -> None:
        super().__init__(f"kapsam disi: {interval}")
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

# Metin eslesmesi SON CAREDIR ve dar tutulur. Durum kodu ANCAK bir HTTP
# baglami ile birlikte gorulurse retry sayilir; ciplak bir sayi aramasi
# "Symbol 500 not found" gibi mesajlarda yanlis eslesirdi.
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


# Gecerli en kucuk HTTP durum kodu. curl_cffi BAGLANTI hatalarina da bir
# Response ilistirir ve status_code'u 0'dir; bu esik olmadan 0 "gecerli bir
# yanit" sayilir, _kind_from_status'un son dalindan DATA olarak doner ve
# olu bir proxy hicbir zaman cezalandirilmazdi (canli kosuda dogrulandi).
_MIN_HTTP_STATUS = 100


def _status_code(exc: BaseException) -> int | None:
    """Istisnanin tasidigi GERCEK HTTP durum kodu; yoksa None."""
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
    """Istisnayi proxy politikasinin anladigi sinifa indirir.

    SIRA BAGLAYICIDIR. curl_cffi'nin RequestException'i OSError
    TUREVIDIR (HTTPError -> RequestException -> CurlError -> OSError);
    bu yuzden bir "OSError -> NETWORK" kurali 403'leri de NETWORK sanar
    ve proxy'yi haksiz cezalandirirdi. HTTP durum kontrolu once gelir.
    """
    # 1) yfinance'in tek tipli rate-limit istisnasi
    if isinstance(exc, yf_exceptions.YFRateLimitError):
        return ErrorKind.RATE_LIMITED

    # 2) HTTP durum kodu (OSError kontrolunden ONCE)
    code = _status_code(exc)
    if code is not None:
        return _kind_from_status(code)

    # 3) yfinance tipleri. YFPricesMissingError "sembol gecersiz" DEGIL
    #    "bu aralikta fiyat yok" demektir (tatil, yeni IPO, kapali borsa)
    #    ve DATA'ya duser; aksi halde her tatil gunu unknown_symbol olurdu.
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

    # 4) Tasima katmani (curl_cffi tipleri)
    if _NETWORK_EXC and isinstance(exc, _NETWORK_EXC):
        return ErrorKind.NETWORK
    if _DATA_EXC and isinstance(exc, _DATA_EXC):
        return ErrorKind.DATA

    # 5) Deterministik programlama/veri hatalari
    if isinstance(exc, _NEVER_RETRYABLE):
        return ErrorKind.DATA

    # 6) Yerlesik ag istisnalari
    if isinstance(exc, TimeoutError | ConnectionError):
        return ErrorKind.NETWORK

    # 7) Metin geri dususu
    text = f"{type(exc).__name__} {exc}".lower()
    if _HTTP_STATUS_RE.search(text) and _HTTP_CONTEXT_RE.search(text):
        return ErrorKind.RATE_LIMITED if "429" in text else ErrorKind.NETWORK
    if any(marker in text for marker in _BLOCKED_MARKERS):
        return ErrorKind.BLOCKED
    if any(marker in text for marker in _RETRYABLE_MARKERS):
        return ErrorKind.NETWORK
    return ErrorKind.DATA


# BLOCKED RETRY EDILMEZ: banlanmis bir proxy'de 5 deneme x jitter'li
# backoff ~30 sn ve token-bucket bosa gider, ustelik saglik durum makinesi
# zaten o proxy'yi cooldown'a alacaktir.
_RETRY_KINDS = frozenset({ErrorKind.RATE_LIMITED, ErrorKind.NETWORK})


def is_retryable(exc: BaseException) -> bool:
    return classify_error(exc) in _RETRY_KINDS


# yfinance trailing veriyi bos oldugunda .iloc ile okur ve bu mesajla patlar
_ABSENT_INDEX_MESSAGE = "positional indexers are out-of-bounds"


def is_absent_data(exc: BaseException) -> bool:
    """ "Bu sembolde bu veri YOK" durumu mu? (S8.2)

    Proje `yf.config.debug.hide_exceptions = False` yapar; bu, gercek
    hatalarin yutulmasini onler ama yfinance'in "404 -> bos sozluk"
    davranisini da ISTISNAYA cevirir. Sirket olmayan sembolde (ETF, fon,
    kripto) Yahoo fundamentals uclarina 404 doner:
    `{"error":{"code":"Not Found","description":"No fundamentals data found
    for symbol: SPY"}}`. Bu bir `empty`tir, `failed` degil.
    """
    if _status_code(exc) == HTTPStatus.NOT_FOUND:
        return True
    return isinstance(exc, IndexError) and _ABSENT_INDEX_MESSAGE in str(exc)
