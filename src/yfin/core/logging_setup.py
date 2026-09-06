"""structlog configuration and the yfinance logging bridge."""

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
            redact_secrets,
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


# --- credential redaction --------------------------------------------------
#
# The proxy DSN is written to yf.config.network.proxy in plain text;
# upstream debug logs or a traceback could print the password. That code
# path is outside our control, so the redaction processor is required.
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


# Fields that must never reach a log line whatever their value. The API
# hands out client secrets and bearer tokens; one careless `log.info(...,
# headers=...)` would park a live credential in a file that outlives it.
# A denylist of names is checked unconditionally rather than trusting
# every future call site to remember.
_SECRET_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "client_secret",
        "secret",
        "access_token",
        "token",
        "password",
    }
)
REDACTED = "***"


def redact_secrets(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if key.lower() in _SECRET_KEYS:
            event_dict[key] = REDACTED
    return event_dict


class _ScrubbingHandler(logging.Handler):
    """Bridges yfinance's stdlib logs into structlog.

    The bridge must exist before yf.config.debug.logging = True is set:
    that assignment triggers _enable_debug_mode(), and yfinance adds its own
    StreamHandler and forces DEBUG level if the logger has no handler yet.
    Attaching first (len(handlers) > 0) avoids duplicate output.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = scrub(record.getMessage())
        except Exception:  # pragma: no cover - a formatting error must not drop the log
            return
        logger = structlog.get_logger("yfinance")
        logger.log(record.levelno, message, logger_name=record.name)


def bridge_yfinance_logging() -> None:
    """Idempotent; attaches a single instance of the bridge to yfinance's logger."""
    yf_logger = logging.getLogger("yfinance")
    if any(isinstance(h, _ScrubbingHandler) for h in yf_logger.handlers):
        return
    yf_logger.addHandler(_ScrubbingHandler())
    # Our own level is filtered on the structlog side; leaving propagate on
    # would make the root handler print the same line a second time.
    yf_logger.propagate = False


def bind_shard_context(run_id: int, shard_index: int, proxy_label: str | None) -> None:
    """Called at the start of a worker thread.

    structlog.contextvars values are not copied into ThreadPoolExecutor
    workers (the executor doesn't use copy_context), and fetch/normalize
    plus yfinance's own logs are produced in exactly those threads, so the
    context is bound again in each thread.
    """
    structlog.contextvars.bind_contextvars(
        run_id=run_id, shard=shard_index, proxy=proxy_label or "direct"
    )
