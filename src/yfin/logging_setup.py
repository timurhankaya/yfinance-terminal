"""structlog yapilandirmasi ve yfinance log koprusu (P6.5)."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level.upper())
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            redact_credentials,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]


# --- kimlik bilgisi redaction (P3.4) ---------------------------------------
#
# Proxy DSN'i yf.config.network.proxy'ye DUZ METIN yazilir; upstream'in
# debug loglari veya bir traceback parolayi basabilir. Bu kod yolu bizim
# kontrolumuzde olmadigi icin redaction processor ZORUNLUDUR.
_CREDENTIAL_RE = re.compile(r"(?P<scheme>\w+://)(?P<user>[^:/@\s]+):[^@/\s]*@")


def scrub(text: str) -> str:
    """scheme://user:pass@host -> scheme://user:***@host"""
    return _CREDENTIAL_RE.sub(r"\g<scheme>\g<user>:***@", text)


def redact_credentials(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key, value in event_dict.items():
        if isinstance(value, str) and "://" in value:
            event_dict[key] = scrub(value)
    return event_dict


class _ScrubbingHandler(logging.Handler):
    """yfinance'in stdlib loglarini structlog'a koprular.

    Koprunun VARLIGI onemlidir: yf.config.debug.logging = True atamasi
    _enable_debug_mode()'u tetikler ve yfinance, logger'da hic handler
    yoksa kendi StreamHandler'ini ekleyip seviyeyi DEBUG'a zorlar. Once
    baglanirsak len(handlers) > 0 olur ve cift cikti olusmaz.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = scrub(record.getMessage())
        except Exception:  # pragma: no cover - formatlama hatasi log'u dusurmesin
            return
        logger = structlog.get_logger("yfinance")
        logger.log(record.levelno, message, logger_name=record.name)


def bridge_yfinance_logging() -> None:
    """Idempotent; yfinance logger'ina koprunun tek ornegini ekler."""
    yf_logger = logging.getLogger("yfinance")
    if any(isinstance(h, _ScrubbingHandler) for h in yf_logger.handlers):
        return
    yf_logger.addHandler(_ScrubbingHandler())
    # Kendi seviyemiz structlog tarafinda suzuluyor; propagate acik kalirsa
    # root handler ayni satiri ikinci kez basar.
    yf_logger.propagate = False


def bind_shard_context(run_id: int, shard_index: int, proxy_label: str | None) -> None:
    """Worker THREAD'inin basinda cagrilir.

    structlog.contextvars degerleri ThreadPoolExecutor worker'larina
    KOPYALANMAZ (executor copy_context kullanmaz) ve fetch/normalize ile
    yfinance'in kendi loglari tam olarak o thread'lerde uretilir; bu
    yuzden baglama her thread'de yeniden yapilir.
    """
    structlog.contextvars.bind_contextvars(
        run_id=run_id, shard=shard_index, proxy=proxy_label or "direct"
    )
