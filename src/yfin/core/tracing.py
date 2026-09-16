"""Traces, and the four places this codebase draws one by hand.

Optional: without the `[otel]` extra or an endpoint everything is a no-op.
`OTEL_SEMCONV_STABILITY_OPT_IN` must be set before any instrumentation import
(read once). No `sampler=` argument: it would silence `OTEL_TRACES_SAMPLER`."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from yfin.core.logging_setup import get_logger

log = get_logger(__name__)

#: The one variable that has to be set before an import rather than before
#: a call. `http` gives the stable HTTP attribute names, `database` the
#: stable SQL ones -- both instrumentations are in the `migration` phase,
#: where the old names are the default.
SEMCONV_VAR = "OTEL_SEMCONV_STABILITY_OPT_IN"
SEMCONV_VALUE = "http,database"

#: Empty means off, which is the default everywhere. The compose override
#: points it at Alloy's OTLP gRPC receiver.
ENDPOINT_VAR = "OTEL_EXPORTER_OTLP_ENDPOINT"

#: The span export gives up after this and drops the batch. Five seconds is
#: longer than any healthy export and shorter than anything a caller would
#: notice, because no caller ever waits for it.
EXPORT_TIMEOUT_MS = 5_000

#: Set once tracing is on, so `span()` can skip the tracer lookup entirely
#: in the overwhelmingly common case where it is not.
_enabled = False


def configure_tracing(service: str) -> bool:
    """Sets up the SDK. Returns whether spans will actually be exported.

    Called once per process, before the first engine is built: an engine
    created earlier is not retroactively traced."""
    global _enabled  # noqa: PLW0603 - one provider per process, by design

    endpoint = os.environ.get(ENDPOINT_VAR, "").strip()
    if not endpoint:
        # Not an error and not a warning: off is the default, and a line
        # about it on every CLI invocation would be noise.
        return False

    # BEFORE the instrumentation imports below. `setdefault`, so an
    # operator who deliberately set something else keeps it.
    os.environ.setdefault(SEMCONV_VAR, SEMCONV_VALUE)

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        # Worth a warning here, unlike the empty endpoint: an endpoint was
        # configured, so somebody expected traces and is not getting any.
        log.warning(
            'tracing endpoint set but the extra is missing; install "yfin[otel]"',
            error=str(exc),
        )
        return False

    provider = TracerProvider(
        resource=Resource.create({"service.name": service}),
        # No `sampler=`: see the module docstring.
    )
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(), export_timeout_millis=EXPORT_TIMEOUT_MS
        )
    )
    trace.set_tracer_provider(provider)
    _enabled = True
    log.info("tracing enabled", service=service, endpoint=endpoint)
    return True


def instrument_fastapi(app: Any) -> None:
    """Traces every request, with the route template as the span name."""
    if not _enabled:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:  # pragma: no cover - guarded by `_enabled`
        return
    FastAPIInstrumentor.instrument_app(app)


def instrument_sqlalchemy(engine: Any) -> None:
    """Traces every statement the engine issues.

    Per engine rather than globally, so engines a test built are not traced.
    psycopg is NOT instrumented: it would nest a duplicate span under every statement."""
    if not _enabled:
        return
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    except ImportError:  # pragma: no cover - guarded by `_enabled`
        return
    SQLAlchemyInstrumentor().instrument(engine=engine)


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """One manual span. A no-op, and a cheap one, when tracing is off.

    Attributes are set at ENTRY, so a span that ends in an exception still
    carries what it was about. `symbol` is allowed here, unlike on a metric:
    a trace is sampled and thrown away, a series is kept forever."""
    if not _enabled:
        yield None
        return
    try:
        from opentelemetry import trace
    except ImportError:  # pragma: no cover - guarded by `_enabled`
        yield None
        return

    tracer = trace.get_tracer("yfin")
    with tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)
        yield current


def set_attributes(current: Any, **attributes: Any) -> None:
    """Adds attributes to a span once they are known.

    `None` is the no-op span, so this is safe to call unconditionally at the
    end of a `with span(...)` block."""
    if current is None:
        return
    with _quiet():
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)


@contextmanager
def _quiet() -> Iterator[None]:
    """Swallows anything the tracing layer raises. Same rule as metrics."""
    try:
        yield
    except Exception as exc:  # noqa: BLE001 - a trace never breaks the work
        log.debug("tracing failed", error=str(exc))


def enabled() -> bool:
    return _enabled


def _reset_for_tests() -> None:
    global _enabled  # noqa: PLW0603 - the tests own this flag
    _enabled = False
