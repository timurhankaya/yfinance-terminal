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
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
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
        # `job_name`, NOT `job`. `job` and `instance` are RESERVED: a
        # scrape stamps them from the scrape config, and a metric that
        # carries its own `job` has it renamed to `exported_job` while
        # `job` becomes the scrape job's name. Measured on the running
        # stack -- every `by (job)` in a dashboard was silently grouping
        # by a label with one value.
        "job_name",
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
        # The exporter's own three. `error_kind` rather than `kind`, which
        # the audit gauges already spend on the scheduled/manual split;
        # `query` for the exporter's self-health; `version` for
        # `yfin_build_info`.
        "error_kind",
        "query",
        "version",
    }
)

MetricKind = Literal["counter", "gauge", "histogram"]

#: Prometheus objects, created on first use. Creating one twice in a process
#: raises "Duplicated timeseries in CollectorRegistry".
_OBJECTS: dict[str, Any] = {}


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


#: What a long-lived process counts about ITSELF, wherever it runs.
_PROCESS_METRICS: dict[str, MetricSpec] = _declare(
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
        labelnames=("version",),
    ),
)


#: What a long-lived SERVICE counts about the work it does.
#:
#: Real Prometheus counters, incremented where the decision is made. They
#: are not in `_SHARD_COUNTERS` because nothing here runs in a process that
#: exits before a scrape: the API, `stream run` and the two relays are all
#: scraped where they stand.
_SERVICE_COUNTERS: dict[str, MetricSpec] = _declare(
    MetricSpec(
        name="yfin_api_ratelimit_decisions_total",
        documentation=(
            "Metering decisions: `allowed`, refused on `rate`, refused on `quota`. "
            "The Lua script's 0/1/2 mapped to words at the metering point."
        ),
        kind="counter",
        labelnames=("reason",),
    ),
    MetricSpec(
        name="yfin_api_concurrency_rejections_total",
        documentation="Requests refused because the plan's in-flight limit was full.",
        kind="counter",
    ),
    MetricSpec(
        name="yfin_api_redis_failopen_total",
        documentation=(
            "Times the counter store was unreachable and the request was let "
            "through unmetered, by which layer gave up."
        ),
        kind="counter",
        labelnames=("where",),
    ),
    MetricSpec(
        name="yfin_api_problems_total",
        documentation="Problem documents returned, by their `type` URN.",
        kind="counter",
        labelnames=("type",),
    ),
    MetricSpec(
        name="yfin_cache_ops_total",
        documentation="In-process cache hits and misses in a long-lived service.",
        kind="counter",
        labelnames=("cache", "result"),
    ),
    # --- `stream run` -----------------------------------------------------
    #
    # These four have gauge counterparts the exporter reads from
    # `stream_sessions` and `stream_connection_health`, and the pair is
    # deliberate: the table says what the CURRENT session has seen, these
    # say what this process has seen since it started. A reconnect storm
    # that ends in a new session shows here and nowhere else.
    MetricSpec(
        name="yfin_stream_messages_total",
        documentation="Messages decoded from the upstream socket.",
        kind="counter",
    ),
    MetricSpec(
        name="yfin_stream_rejects_total",
        documentation="Ticks refused, by why. `unknown_symbol` is the FK filter.",
        kind="counter",
        labelnames=("reason",),
    ),
    MetricSpec(
        name="yfin_stream_reconnects_total",
        documentation="Upstream reconnects. Survives the session the table's count does not.",
        kind="counter",
    ),
    MetricSpec(
        name="yfin_stream_batch_seconds",
        documentation=(
            "One write batch, end to end. The number the 250 ms batch "
            "interval has to stay under for the queue not to grow."
        ),
        kind="histogram",
    ),
    MetricSpec(
        name="yfin_stream_copy_rows_total",
        documentation="Rows verified into each table by the COPY path.",
        kind="counter",
        labelnames=("table",),
    ),
    # Counted per BATCH, not per tick: the question this answers is whether
    # the browser fan-out is working, and one failed batch is one failure
    # whether it carried 1 tick or 500. `disabled` is a real result rather
    # than an absent series -- "nobody is publishing" and "publishing is
    # broken" look identical on a graph that only counts failures.
    MetricSpec(
        name="yfin_stream_publish_total",
        documentation="Tick batches offered to the browser fan-out, by outcome.",
        kind="counter",
        labelnames=("result",),
    ),
    # --- the relays -------------------------------------------------------
    #
    # `outbox` is the table name, which is what tells the two relays apart:
    # they are separate processes running the same code, and a metric
    # without it would sum a tick backlog into a change backlog.
    MetricSpec(
        name="yfin_relay_published_total",
        documentation="Messages acknowledged by the broker, by outbox.",
        kind="counter",
        labelnames=("outbox",),
    ),
    MetricSpec(
        name="yfin_relay_failures_total",
        documentation=(
            "Passes that ended with the offset NOT advanced. The rows are "
            "retried, so this counts refusals to lose them, not lost rows."
        ),
        kind="counter",
        labelnames=("outbox",),
    ),
    MetricSpec(
        name="yfin_relay_pass_seconds",
        documentation="One read-publish-advance pass, by outbox.",
        kind="histogram",
        labelnames=("outbox",),
    ),
    MetricSpec(
        name="yfin_relay_chunks_dropped_total",
        documentation=(
            "Outbox chunks dropped after every row in them was published. "
            "At 1.5 billion rows a year this is what keeps the queue affordable."
        ),
        kind="counter",
        labelnames=("outbox",),
    ),
)


#: What a sync SHARD accumulates in memory and flushes into `run_metrics`.
#:
#: Every one of these is republished by the exporter as a gauge -- see
#: `_republished` below -- so the list is written once and the two names
#: cannot drift apart.
_SHARD_COUNTERS: dict[str, MetricSpec] = _declare(
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


def exported_name(counter: str) -> str:
    """The gauge the exporter republishes a shard counter under.

    `yfin_sync_retries_total` -> `yfin_sync_retries`. The suffix goes
    because the published value is not a monotonic total of the scheduler
    process at all -- it is the latest run's count, summed over that run's
    shards, and it drops back when the next run does less work. Keeping
    `_total` would also register one name twice with two label sets, since
    the exported gauge carries `scope` and the counter does not.
    """
    return counter.removesuffix("_total")


def _republished(counters: dict[str, MetricSpec]) -> dict[str, MetricSpec]:
    """A gauge per shard counter, with `scope` in front of its labels."""
    return _declare(
        *(
            MetricSpec(
                name=exported_name(spec.name),
                documentation=(
                    f"{spec.documentation} Latest run per scope, summed over its shards."
                ),
                kind="gauge",
                labelnames=("scope", *spec.labelnames),
            )
            for spec in counters.values()
        )
    )


#: What the exporter reads out of the database every
#: `yf_exporter_interval_seconds` and republishes on the scheduler's
#: `/metrics`. All gauges: a scrape reads the last refresh's value and
#: never touches the database.
_EXPORTER_GAUGES: dict[str, MetricSpec] = _declare(
    # --- freshness --------------------------------------------------------
    MetricSpec(
        name="yfin_cells_total",
        documentation=(
            "Cells -- (symbol, region, dataset) -- in the universe, by scope "
            "and dataset. A cell leaves the universe when its latest status "
            "is out_of_scope or unknown_symbol."
        ),
        kind="gauge",
        labelnames=("scope", "dataset"),
    ),
    MetricSpec(
        name="yfin_cells_stale",
        documentation=(
            "Cells whose latest ok/empty/skipped run is older than "
            "yf_freshness_factor times the interval of the job that writes them."
        ),
        kind="gauge",
        labelnames=("scope", "dataset"),
    ),
    MetricSpec(
        name="yfin_intraday_scope_stale",
        documentation=(
            "Symbols in intraday_scope whose newest bar is within "
            "yf_intraday_retention_warn_days of that interval's Yahoo limit."
        ),
        kind="gauge",
        labelnames=("interval",),
    ),
    # --- correctness ------------------------------------------------------
    MetricSpec(
        name="yfin_audit_items",
        documentation="Items of the latest run per scope and kind, by status.",
        kind="gauge",
        labelnames=("scope", "kind", "status"),
    ),
    MetricSpec(
        name="yfin_audit_errors",
        documentation="Failed items of the latest run per scope and kind, by error kind.",
        kind="gauge",
        labelnames=("scope", "kind", "error_kind"),
    ),
    MetricSpec(
        name="yfin_audit_rows",
        documentation="Rows the latest run per scope and kind fetched, wrote, verified, skipped.",
        kind="gauge",
        labelnames=("scope", "kind", "measure"),
    ),
    MetricSpec(
        name="yfin_audit_status",
        documentation="Latest FINISHED run per scope and kind: 0 ok, 1 partial, 2 failed.",
        kind="gauge",
        labelnames=("scope", "kind"),
    ),
    MetricSpec(
        name="yfin_audit_running",
        documentation="1 while a run of this scope is in flight.",
        kind="gauge",
        labelnames=("scope",),
    ),
    MetricSpec(
        name="yfin_audit_started_timestamp",
        documentation="When the latest run per scope and kind started, as a unix timestamp.",
        kind="gauge",
        labelnames=("scope", "kind"),
    ),
    MetricSpec(
        name="yfin_proxies",
        documentation=(
            "Proxies by state: `disabled` is the operator's decision, the rest "
            "is the health the system observed."
        ),
        kind="gauge",
        labelnames=("state",),
    ),
    # --- bars -------------------------------------------------------------
    MetricSpec(
        name="yfin_bar_gaps_open",
        documentation="Unresolved bar gaps, by interval and why they were recorded.",
        kind="gauge",
        labelnames=("interval", "reason"),
    ),
    MetricSpec(
        name="yfin_bar_gaps_oldest_age_seconds",
        documentation="Age of the oldest unresolved gap, by interval.",
        kind="gauge",
        labelnames=("interval",),
    ),
    MetricSpec(
        name="yfin_bar_gaps_expiring",
        documentation=(
            "Open gaps within yf_intraday_retention_warn_days of the interval's "
            "Yahoo limit -- after that edge Yahoo can never serve the window again."
        ),
        kind="gauge",
        labelnames=("interval",),
    ),
    MetricSpec(
        name="yfin_bar_gaps_resolved",
        documentation="Gaps closed so far, by interval and what closed them.",
        kind="gauge",
        labelnames=("interval", "resolved_by"),
    ),
    MetricSpec(
        name="yfin_bar_rescales_pending",
        documentation="Splits that apply to an existing archive and have not been applied.",
        kind="gauge",
    ),
    # --- stream -----------------------------------------------------------
    MetricSpec(
        name="yfin_stream_connections",
        documentation="Upstream stream connections by state.",
        kind="gauge",
        labelnames=("state",),
    ),
    MetricSpec(
        name="yfin_stream_heartbeat_age_seconds",
        documentation="Oldest connection heartbeat. A stale one means the writer stopped.",
        kind="gauge",
    ),
    MetricSpec(
        name="yfin_stream_canary_age_seconds",
        documentation=(
            "Oldest canary. The canary sits at the end of the subscription list, "
            "so silence here is the only signal Yahoo truncated it."
        ),
        kind="gauge",
    ),
    MetricSpec(
        name="yfin_stream_subscribed_symbols",
        documentation="Symbols actually subscribed, summed over connections.",
        kind="gauge",
    ),
    MetricSpec(
        name="yfin_stream_reconnects",
        documentation=(
            "Reconnects of the current connections. The in-process `_total` "
            "counter is the one that survives a session."
        ),
        kind="gauge",
    ),
    MetricSpec(
        name="yfin_stream_messages",
        documentation="Messages received by the open stream session.",
        kind="gauge",
    ),
    MetricSpec(
        name="yfin_stream_rejects",
        documentation="Rows the open stream session rejected.",
        kind="gauge",
    ),
    # --- outboxes ---------------------------------------------------------
    MetricSpec(
        name="yfin_outbox_unpublished_rows",
        documentation="Rows the relay has not published yet, by outbox table.",
        kind="gauge",
        labelnames=("outbox",),
    ),
    MetricSpec(
        name="yfin_outbox_oldest_age_seconds",
        documentation="Age of the oldest unpublished row, by outbox table.",
        kind="gauge",
        labelnames=("outbox",),
    ),
    # --- API usage --------------------------------------------------------
    MetricSpec(
        name="yfin_api_usage_requests",
        documentation="Requests on the most recent FLUSHED day, by endpoint family.",
        kind="gauge",
        labelnames=("family",),
    ),
    MetricSpec(
        name="yfin_api_usage_day_timestamp",
        documentation="Which day yfin_api_usage_requests is about, as a unix timestamp.",
        kind="gauge",
    ),
    MetricSpec(
        name="yfin_api_usage_estimated_days",
        documentation="Days flagged as reconstructed because the counters were unreachable.",
        kind="gauge",
    ),
    # --- the scheduler's own jobs -----------------------------------------
    MetricSpec(
        name="yfin_job_last_success_timestamp",
        documentation=(
            "When this job last finished ok. Seeded from scheduler_runs at "
            "start-up, so a restart does not look like an overdue job."
        ),
        kind="gauge",
        labelnames=("job_name",),
    ),
    MetricSpec(
        name="yfin_job_last_duration_seconds",
        documentation="How long this job's last firing took.",
        kind="gauge",
        labelnames=("job_name",),
    ),
    MetricSpec(
        name="yfin_job_interval_seconds",
        documentation="The cron's mean period; what the freshness and overdue rules divide by.",
        kind="gauge",
        labelnames=("job_name",),
    ),
    MetricSpec(
        name="yfin_job_running",
        documentation="1 while this job's subprocess is alive.",
        kind="gauge",
        labelnames=("job_name",),
    ),
    MetricSpec(
        name="yfin_job_next_run_timestamp",
        documentation="When this job fires next, as a unix timestamp.",
        kind="gauge",
        labelnames=("job_name",),
    ),
    MetricSpec(
        name="yfin_job_runs_total",
        documentation="Firings by result, including the ones that never became a subprocess.",
        kind="counter",
        labelnames=("job_name", "result"),
    ),
    # --- the exporter's own health ----------------------------------------
    MetricSpec(
        name="yfin_exporter_query_seconds",
        documentation="How long the last refresh of this query took.",
        kind="gauge",
        labelnames=("query",),
    ),
    MetricSpec(
        name="yfin_exporter_last_success_timestamp",
        documentation=(
            "Last refresh in which every query succeeded. A failing query keeps "
            "the previous gauges and leaves this behind."
        ),
        kind="gauge",
    ),
)


#: Everything this codebase counts.
#:
#: The `yfin_sync_` prefix marks a counter a SHARD accumulates and the
#: exporter republishes from `run_metrics`; `yfin_audit_` marks a gauge the
#: exporter reads from the audit tables. The two prefixes are separate so no
#: name is ever registered twice with two different label sets.
METRICS: dict[str, MetricSpec] = {
    **_PROCESS_METRICS,
    **_SERVICE_COUNTERS,
    **_SHARD_COUNTERS,
    **_republished(_SHARD_COUNTERS),
    **_EXPORTER_GAUGES,
}


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
    if accumulator is not None:
        with suppress(Exception):
            accumulator.inc(name, amount, **labels)
        return
    with suppress(Exception):
        counter = _object(name)
        (counter.labels(**labels) if labels else counter).inc(amount)


def _object(name: str) -> Any:
    """The Prometheus object for a declared metric, created on first use.

    Raises when the extra is not installed -- every caller here is already
    inside a `suppress`, because metrics are optional everywhere and a
    missing extra must not stop a run.
    """
    existing = _OBJECTS.get(name)
    if existing is not None:
        return existing
    spec = METRICS[name]
    from prometheus_client import Counter, Gauge, Histogram

    if spec.kind == "histogram":
        # The library's default buckets, which run to 10 seconds. Both
        # histograms here measure a pass that is supposed to take
        # milliseconds, and the tail is exactly the part worth seeing.
        created: Any = Histogram(spec.name, spec.documentation, spec.labelnames)
    elif spec.kind == "gauge":
        # `multiprocess_mode="max"` costs nothing in a single-process
        # service and is what makes the gauge readable at all if one ever
        # runs under `PROMETHEUS_MULTIPROC_DIR`.
        created = Gauge(
            spec.name, spec.documentation, spec.labelnames, multiprocess_mode="max"
        )
    else:
        created = Counter(spec.name, spec.documentation, spec.labelnames)
    _OBJECTS[name] = created
    return created


def observe(name: str, value: float, **labels: str) -> None:
    """Records one measurement in a histogram.

    Never raises, like `inc`. A batch that failed to be timed is still a
    batch that was written, and the timing is worth less than the write.

    There is no accumulator branch: `run_metrics` stores integers keyed by
    name and labels, which is a counter's shape and not a histogram's. A
    shard that needs a duration puts it in `sync_run_items.duration_ms`,
    where it already goes.
    """
    with suppress(Exception):
        _validate(name, labels)
        histogram = _object(name)
        (histogram.labels(**labels) if labels else histogram).observe(value)


@contextmanager
def timed(name: str, **labels: str) -> Iterator[None]:
    """Times the block and records it. Records even when the block raises.

    A pass that failed is still a pass that took time, and dropping its
    duration would make the histogram flatter exactly when something is
    going wrong.
    """
    started = time.monotonic()
    try:
        yield
    finally:
        observe(name, time.monotonic() - started, **labels)


def set_gauge(name: str, value: float, **labels: str) -> None:
    """Sets a gauge the exporter read out of the database.

    Never raises, for the same reason `inc` does not: this runs on the
    scheduler's exporter thread, and the scheduler is the process that runs
    the jobs. A number nobody could publish is worth less than the work.

    There is no accumulator branch. A gauge is a value read from a table by
    a long-lived process; a shard has nothing to put in one.
    """
    with suppress(Exception):
        _validate(name, labels)
        gauge = _object(name)
        (gauge.labels(**labels) if labels else gauge).set(value)


def clear_gauge(name: str) -> None:
    """Drops every label combination a gauge currently carries.

    Called by the exporter before it republishes a query's gauges, and only
    when that query SUCCEEDED. Without it a cell that left the universe, a
    proxy that was deleted or a stream connection that closed would keep the
    last value it ever had, on a dashboard, forever. With it, a query that
    fails does not clear -- so the previous refresh's numbers stand and
    `yfin_exporter_last_success_timestamp` is what goes stale.
    """
    with suppress(Exception):
        _object(name).clear()


def set_build_info(version: str) -> None:
    """`yfin_build_info{version} 1`, so a dashboard can say what is running.

    A `Gauge` with the value 1 rather than an `Info`: `Info` does not work
    in multiprocess mode, and the API runs four uvicorn workers.
    """
    set_gauge("yfin_build_info", 1, version=version)


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
    "clear_gauge",
    "count_exception",
    "current_accumulator",
    "exported_name",
    "inc",
    "label_key",
    "observe",
    "serve_metrics",
    "set_build_info",
    "set_gauge",
    "timed",
    "use_accumulator",
]
