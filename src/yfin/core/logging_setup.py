"""One log pipeline, two renderings, and the redaction that survives both.

Every record, structlog or stdlib, goes through ONE chain and ONE handler:
redaction is a processor, so a record on another route would be unredacted.
Logs stay on stderr because several commands print machine-readable JSON to stdout."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

#: The service name every line carries. Module state rather than a
#: contextvar: `ThreadPoolExecutor` does not copy the context into its
#: workers -- see `bind_shard_context` -- so a `service` bound with
#: `bind_contextvars` would be absent from exactly the fetch and normalise
#: lines a dashboard filters by service to find.
_service = ""

#: Formats `fmt` accepts. Anything else falls back to the TTY default
#: rather than raising: a mistyped `LOG_FORMAT` must not stop a sync.
FORMATS = ("console", "json")


def _resolve_format(fmt: str) -> str:
    """`console` on a TTY, `json` otherwise, unless told."""
    if fmt in FORMATS:
        return fmt
    return "console" if sys.stderr.isatty() else "json"


def configure_logging(level: str = "INFO", fmt: str = "", service: str = "") -> None:
    """Installs the chain. Idempotent, and safe to call again with new values.

    Called more than once per process, so it replaces the handler rather than
    adding a second one. `service` is what a dashboard filters by."""
    global _service  # noqa: PLW0603 - one service name per process, by design
    _service = service or _service

    resolved = _resolve_format(fmt)
    shared = _shared_processors()

    structlog.configure(
        processors=[
            # Drops a line below the threshold before any processor runs.
            # Without it, a DEBUG call at INFO would still pay for
            # timestamping and redaction on the way to being discarded.
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        # False on purpose. A cached logger keeps the chain it was built
        # with, and this function is called AGAIN -- by `create_app`, by a
        # shard after it reads its settings -- with a different format.
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        # The same list the structlog calls went through, for records that
        # never touched structlog: yfinance's, SQLAlchemy's, uvicorn's.
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            *_rendering(resolved),
        ],
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())


def _shared_processors() -> list[Any]:
    """The chain both structlog and stdlib records run through.

    Redaction comes FIRST, before anything can copy a value where later
    processors do not look; `_add_trace_context` last, after the exception."""
    return [
        structlog.contextvars.merge_contextvars,
        redact_secrets,
        redact_credentials,
        _add_service,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _add_trace_context,
    ]


def _rendering(fmt: str) -> list[Any]:
    """How a line is turned into text, and how a traceback is turned with it.

    Exception handling lives here because the two renderers want it in
    different shapes. `show_locals=False` on both: the redaction processors
    only see top-level event keys, so a password in a frame local would leak."""
    if fmt == "json":
        return [
            structlog.processors.ExceptionRenderer(
                structlog.tracebacks.ExceptionDictTransformer(show_locals=False)
            ),
            structlog.processors.JSONRenderer(),
        ]
    return [
        structlog.dev.ConsoleRenderer(
            colors=sys.stderr.isatty(),
            exception_formatter=structlog.dev.plain_traceback,
        )
    ]


def _add_service(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """`service` on every line, including the ones from worker threads."""
    if _service:
        event_dict.setdefault("service", _service)
    return event_dict


def _add_trace_context(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """`trace_id` and `span_id` when a span is active; nothing otherwise.

    Nothing, not zeros: Grafana's `derivedFields` would turn the all-zero
    invalid span id into a link. OTel is the `[otel]` extra, imported lazily."""
    if _otel_missing:
        return event_dict
    try:
        # The function, not the module: `opentelemetry` is a namespace
        # package, and importing `trace` from it leaves mypy resolving the
        # name against the namespace rather than against `opentelemetry-api`.
        from opentelemetry.trace import get_current_span
    except ImportError:  # pragma: no cover - depends on the extra
        _remember_otel_is_missing()
        return event_dict

    context = get_current_span().get_span_context()
    if not context.is_valid:
        return event_dict
    event_dict["trace_id"] = format(context.trace_id, "032x")
    event_dict["span_id"] = format(context.span_id, "016x")
    return event_dict


#: Set once when the extra turns out to be absent, so the import is not
#: retried on every log line for the life of the process.
_otel_missing = False


def _remember_otel_is_missing() -> None:
    global _otel_missing  # noqa: PLW0603 - a one-way latch
    _otel_missing = True


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


def bridge_yfinance_logging() -> None:
    """Points yfinance's logger at the root chain. Idempotent.

    The `NullHandler` is a decoy: yfinance's `_enable_debug_mode()` installs
    its own unredacted `StreamHandler` when the logger has no handler.
    `propagate = True` then carries the record to the root handler."""
    yf_logger = logging.getLogger("yfinance")
    if not any(isinstance(h, logging.NullHandler) for h in yf_logger.handlers):
        yf_logger.addHandler(logging.NullHandler())
    yf_logger.propagate = True


def bind_shard_context(run_id: int, shard_index: int, proxy_label: str | None) -> None:
    """Called at the start of a worker thread.

    structlog.contextvars values are not copied into ThreadPoolExecutor
    workers, so the context is bound again in each thread."""
    structlog.contextvars.bind_contextvars(
        run_id=run_id, shard=shard_index, proxy=proxy_label or "direct"
    )


__all__ = [
    "FORMATS",
    "REDACTED",
    "bind_shard_context",
    "bridge_yfinance_logging",
    "configure_logging",
    "get_logger",
    "redact_credentials",
    "redact_secrets",
    "scrub",
]
