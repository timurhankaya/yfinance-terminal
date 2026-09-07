"""One log pipeline, two renderings, and the redaction that survives both.

Everything this process logs -- structlog calls, yfinance's stdlib records,
SQLAlchemy's, uvicorn's -- goes through ONE chain and comes out of ONE
handler. That is not tidiness. Redaction is a processor, so a record that
took a different route would be a record nobody redacted, and the one thing
this codebase must never write is the proxy DSN it is handed in plain text.

The plumbing is structlog's stdlib recipe: `LoggerFactory` and
`BoundLogger`, a chain ending in `wrap_for_formatter`, and a root
`StreamHandler` whose `ProcessorFormatter` carries the same processors as
`foreign_pre_chain`. A stdlib record and a structlog call meet at the
formatter and are rendered by the same code.

**Logs stay on stderr.** `yfin config export`, `config schema --json` and
`scripts/dump_openapi.py` print machine-readable JSON to stdout, and
logging is configured before they run -- a log line on stdout would corrupt
them. Alloy's `loki.source.docker` reads both streams and labels them, so
routing to stderr loses nothing.

`show_locals` is off in both renderings. The default serialises frame
locals into the traceback, and a `settings` or `dsn` local would carry a
password past a redaction that only ever sees top-level event keys.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

#: The service name every line carries. Module state rather than a
#: contextvar, and the difference is load-bearing: `ThreadPoolExecutor` does
#: not copy the context into its workers -- `bind_shard_context` exists
#: because the project already hit that -- so a `service` bound with
#: `bind_contextvars` would be absent from exactly the fetch and normalise
#: lines that a dashboard filters by service to find.
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

    Called at every entry point, and more than once in a process: a CLI
    command configures, and so does `create_app`. Reconfiguring has to
    replace the handler rather than add a second one, or every line would
    appear twice.

    `service` is what a dashboard filters by, so a subprocess passes its
    own: a shard is `sync`, not the scheduler that started it.
    """
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

    Order matters in two places. Redaction comes FIRST, before anything can
    copy a value somewhere the later processors do not look. And
    `_add_trace_context` comes last, after the exception has been rendered,
    so a line carrying a traceback still carries the trace it belongs to.
    """
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

    The exception handling lives here rather than in the shared chain
    because the two renderers want it in different shapes: `ConsoleRenderer`
    formats `exc_info` itself, and `JSONRenderer` needs it already turned
    into data. `ProcessorFormatter` has moved `record.exc_info` into the
    event dict by the time these run, so a stdlib record's traceback is
    rendered by the same code as a structlog one's.

    `show_locals=False` on both sides, and it is the whole reason this is
    spelled out. The default serialises every frame local into the
    traceback -- a `settings` object, a `dsn` string -- and the redaction
    processors only ever see top-level event keys, so a password in a local
    would travel to Loki untouched.
    """
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

    Nothing, not zeros: an all-zero id is what OpenTelemetry returns for
    the invalid span, and Grafana's `derivedFields` would happily turn it
    into a link to a trace that does not exist.

    Imported inside the function because the OTel packages are the `[otel]`
    extra. Without them this is a no-op and costs one `ImportError` per
    line -- which is why the miss is remembered.
    """
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

    Two lines, and both are load-bearing.

    The `NullHandler` is a DECOY. `yf.config.debug.logging = True` runs
    `_enable_debug_mode()`, which checks whether the logger has a handler
    and, finding none, installs its own `StreamHandler` -- a second,
    unredacted route to stderr for exactly the records most likely to carry
    a DSN. Giving it a handler first makes that check pass without adding
    an output.

    `propagate = True` is what then carries the record to the root handler,
    where the shared chain redacts and renders it like everything else.
    The old bridge set it to False and re-emitted through a structlog call
    of its own, which meant yfinance's records were formatted by a second
    code path -- one that could drift from this one, and did not carry
    `service` or the trace context.
    """
    yf_logger = logging.getLogger("yfinance")
    if not any(isinstance(h, logging.NullHandler) for h in yf_logger.handlers):
        yf_logger.addHandler(logging.NullHandler())
    yf_logger.propagate = True


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
