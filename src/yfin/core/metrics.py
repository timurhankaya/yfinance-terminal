"""Counters, and the two very different processes that keep them.

A long-lived service -- the API, `stream run`, either relay, the scheduler
-- serves `/metrics` and Prometheus scrapes it. A sync shard cannot: it is a
short-lived process that exits long before any scrape reaches it, and a
Pushgateway would keep its series forever, has no `up`, and is one more
thing to lose. So a shard accumulates its counters in memory and flushes
them into `run_metrics` on the way out, and the exporter reads the database.

Both are the same call at the point of instrumentation. `readers.py`
counting a cache hit has no business knowing which kind of process it is in.

Two rules the whole namespace obeys, and both are enforced below rather than
asked for in a review:

**No `symbol` label, ever.** Ten thousand symbols times forty-nine datasets
is a series explosion that would cost more than the pipeline it measures.
Symbol-level detail lives in the database and in the logs.

**Every metric is declared.** A counter created at its call site has no
documentation and no label contract, and a typo in a label name becomes a
second series that looks like data. `METRICS` is the declaration; the
accumulator refuses anything it does not know.

`prometheus_client` is imported LAZILY and never at module level.
`prometheus_client` picks its value class from `PROMETHEUS_MULTIPROC_DIR` at
IMPORT time, process-wide, so importing it before the environment is final
would decide multiprocess mode for a process that is not the API. It is also
an extra, and a missing extra must not stop a sync.
"""

from __future__ import annotations

import json
import threading
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal

from yfin.core.logging_setup import get_logger

log = get_logger(__name__)

#: The closed label set. A label outside it is a typo or a dimension nobody
#: agreed to pay for; `symbol` is absent on purpose and a test says so.
ALLOWED_LABELS: frozenset[str] = frozenset(
    {
        "dataset",
        "scope",
        "kind",
        "interval",
        "reason",
        "state",
        "job",
        "result",
        "handler",
        "outbox",
        "cache",
        "where",
        "type",
        "status",
        "op",
        "table",
        "outcome",
        "family",
        "measure",
        "resolved_by",
    }
)

MetricKind = Literal["counter", "gauge", "histogram"]

#: Prometheus objects, created on first use. Creating one twice in a process
#: raises "Duplicated timeseries in CollectorRegistry".
_COUNTERS: dict[str, Any] = {}


@dataclass(frozen=True)
class MetricSpec:
    """One declared metric.

    `name` carries the `yfin_` namespace and the unit suffix in full, so
    grepping for the string a dashboard uses finds the declaration.
    """

    name: str
    documentation: str
    kind: MetricKind
    labelnames: tuple[str, ...] = ()


def _declare(*specs: MetricSpec) -> dict[str, MetricSpec]:
    return {spec.name: spec for spec in specs}


#: Everything this codebase counts.
#:
#: The `yfin_sync_` prefix marks a counter a SHARD accumulates and the
#: exporter republishes from `run_metrics`; `yfin_audit_` marks a gauge the
#: exporter reads from the audit tables. The two prefixes are separate so no
#: name is ever registered twice with two different label sets.
METRICS: dict[str, MetricSpec] = _declare(
    MetricSpec(
        name="yfin_exceptions_total",
        documentation="Exceptions caught at a boundary, by class name.",
        kind="counter",
        labelnames=("type",),
    ),
    MetricSpec(
        name="yfin_build_info",
        documentation="Always 1; carries the running version as a label.",
        kind="gauge",
        labelnames=("type",),
    ),
    # --- what a sync shard accumulates ------------------------------------
    MetricSpec(
        name="yfin_sync_yahoo_requests_total",
        documentation="Upstream requests, by dataset and how they ended.",
        kind="counter",
        labelnames=("dataset", "outcome"),
    ),
    MetricSpec(
        name="yfin_sync_yahoo_errors_total",
        documentation="Upstream failures, by the kind `classify_error` gave them.",
        kind="counter",
        labelnames=("kind",),
    ),
    MetricSpec(
        name="yfin_sync_retries_total",
        documentation="Retries, by the kind of error that caused them.",
        kind="counter",
        labelnames=("kind",),
    ),
    MetricSpec(
        name="yfin_sync_cache_ops_total",
        documentation="In-process cache hits and misses.",
        kind="counter",
        labelnames=("cache", "result"),
    ),
    MetricSpec(
        name="yfin_sync_proxy_turns_total",
        documentation="Proxy outcomes, as the tracker recorded them.",
        kind="counter",
        labelnames=("result",),
    ),
    MetricSpec(
        name="yfin_sync_write_rows_total",
        documentation=(
            "Rows the writer attempted, verified and skipped, by table. "
            "`attempted` is the distinct-key count it proposes."
        ),
        kind="counter",
        labelnames=("table", "op"),
    ),
)


def label_key(labels: dict[str, str]) -> str:
    """Labels as one string, canonically.

    This is a PRIMARY KEY component on `run_metrics`, so two increments of
    the same counter have to produce the same bytes whatever order the
    keywords were written in. Sorted keys and compact separators; `{}` for
    no labels, because the column is NOT NULL and an empty string would read
    as something forgotten rather than as "this counter has none".
    """
    return json.dumps(labels, sort_keys=True, separators=(",", ":"))


def _validate(name: str, labels: dict[str, str]) -> MetricSpec:
    spec = METRICS[name]
    unknown = set(labels) - set(spec.labelnames)
    if unknown:
        raise ValueError(
            f"{name}: label(s) {sorted(unknown)} are not declared for it "
            f"(declared: {list(spec.labelnames)}). A label nobody declared is a "
            "typo or a dimension nobody agreed to pay for."
        )
    return spec


@dataclass(frozen=True)
class CounterRow:
    """One accumulated counter, ready for `run_metrics`."""

    name: str
    labels: str
    value: int


class Accumulator:
    """Counters for a process that will not be scraped.

    A sync shard fills one of these and flushes it into `run_metrics` in its
    own short transaction just before it exits -- outside the symbol
    transactions, so a metrics failure can never roll back data.

    Locked, because worker threads count Yahoo requests concurrently. A lost
    increment is a silently wrong number, which is worse than no number.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[tuple[str, str], int] = {}

    def inc(self, name: str, amount: int = 1, **labels: str) -> None:
        _validate(name, labels)
        key = (name, label_key(labels))
        with self._lock:
            self._values[key] = self._values.get(key, 0) + amount

    def rows(self) -> list[CounterRow]:
        with self._lock:
            return [
                CounterRow(name=name, labels=labels, value=value)
                for (name, labels), value in sorted(self._values.items())
            ]

    def clear(self) -> None:
        with self._lock:
            self._values.clear()


#: The accumulator this process counts into, if it is the kind of process
#: that cannot be scraped. A module-level handle rather than a parameter
#: threaded through five layers: `storage/contracts.apply_write` counts rows
#: and has no business knowing what a shard is, and `core/config` is exactly
#: the dependency `storage/persistence.py` states it does not have.
_accumulator: Accumulator | None = None


def use_accumulator(accumulator: Accumulator | None) -> None:
    """Routes `inc` into this accumulator for the rest of the process.

    Called once by a shard, or by the single-process path, right after the
    settings are known. `None` puts the process back on the Prometheus
    counters, which is what a long-lived service uses.
    """
    global _accumulator  # noqa: PLW0603 - one handle per process, by design
    _accumulator = accumulator


def current_accumulator() -> Accumulator | None:
    return _accumulator


def inc(name: str, amount: int = 1, **labels: str) -> None:
    """Counts one thing, wherever this process keeps its counters.

    The same call in a shard and in the API. A shard accumulates in memory
    and flushes to `run_metrics` on the way out; a long-lived service
    increments a Prometheus counter that a scrape will read.

    Never raises. Instrumentation that can fail is instrumentation that
    turns a working sync into a broken one, and the numbers are worth less
    than the run.
    """
    accumulator = _accumulator
    # `suppress` rather than a bare except: see the docstring. Every failure
    # here -- an undeclared metric, a missing extra, a registry clash -- is
    # worth less than the run it would otherwise take down.
    if accumulator is not None:
        with suppress(Exception):
            accumulator.inc(name, amount, **labels)
        return
    with suppress(Exception):
        counter = _counter(name)
        if counter is not None:
            (counter.labels(**labels) if labels else counter).inc(amount)


def _counter(name: str) -> Any:
    """The Prometheus counter for a declared metric, created on first use.

    Creating one twice in a process raises "Duplicated timeseries in
    CollectorRegistry", so they are cached. Returns None when the extra is
    not installed, which is not an error: metrics are optional everywhere.
    """
    existing = _COUNTERS.get(name)
    if existing is not None:
        return existing
    spec = METRICS[name]
    from prometheus_client import Counter

    counter = Counter(spec.name, spec.documentation, spec.labelnames)
    _COUNTERS[name] = counter
    return counter


def serve_metrics(port: int, addr: str = "0.0.0.0") -> bool:  # noqa: S104
    """Starts the `/metrics` endpoint in a daemon thread. Returns whether it
    is listening.

    `port = 0` means off, which is the default: `METRICS_PORT` is env-only
    because one value in the settings table would bind five services to one
    port.

    Every failure here is a WARNING and never an exception. A metrics port
    already in use, or the extra not installed, must not stop a sync -- the
    whole point of the port is to observe the work, not to gate it.
    """
    if not port:
        return False
    try:
        from prometheus_client import start_http_server

        start_http_server(port, addr=addr)
    except Exception as exc:  # noqa: BLE001 - observability never blocks the work
        log.warning("metrics endpoint not started", port=port, error=str(exc))
        return False
    log.info("metrics endpoint listening", port=port)
    return True


def count_exception(exc: BaseException) -> None:
    """Increments `yfin_exceptions_total{type}` with the exception's class.

    `prometheus_client.count_exceptions` cannot label by type, and the type
    is the only thing that makes the counter worth reading.

    It runs inside exception handlers, so it swallows its own failures: a
    metric that raised there would replace the real error with itself.
    """
    inc("yfin_exceptions_total", type=type(exc).__name__)


__all__ = [
    "ALLOWED_LABELS",
    "METRICS",
    "Accumulator",
    "CounterRow",
    "MetricSpec",
    "count_exception",
    "current_accumulator",
    "inc",
    "label_key",
    "serve_metrics",
    "use_accumulator",
]
