# Observability: scheduler, metrics, logs, traces and dashboards

Status: approved, not yet implemented
Date: 2026-09-07
Sibling of `2026-09-07-pipeline-change-events-design.md`; the outbox
metrics below cover both `stream_outbox` and the `pipeline_outbox` that
design introduces. Neither depends on the other being implemented first.

## Why

The pipeline records a great deal about itself -- `sync_runs`,
`sync_run_items`, `bar_gaps`, `stream_connection_health`,
`stream_sessions`, `api_usage_daily`, the relay offsets -- and exposes
almost none of it. `yfin status` prints the last run; `yfin stream
status` prints connection rows and relay lag; everything else is a SQL
query someone has to remember to write. There is no time series, no
alert, no log search, and no scheduler: README says operating this well
needs "a scheduler that does not miss the 29-day window on 1-minute
bars", and the repository does not provide one.

This design adds the scheduler, a metrics surface for every process, JSON
logs, traces, and a committed Grafana stack, so that health, correctness
and freshness are visible without opening `psql`.

### What exists, verified 2026-09-07

| Fact | Where |
| --- | --- |
| structlog with `ConsoleRenderer`; secrets and credentials redacted; yfinance's stdlib logs bridged | `core/logging_setup.py:14-113` |
| `run_id`, `shard`, `proxy` bound as contextvars, rebound in worker threads | `core/logging_setup.py:116-126` |
| API logs one line per request with `request_id`, route template, status, duration; never the query string | `api/core/middleware.py:100-125` |
| `/health` and `/health/ready`, unauthenticated, own per-IP window and readiness cache | `api/routers/meta.py:45-149` |
| No Prometheus, OpenTelemetry or Sentry anywhere; no `/metrics` | grep |
| No scheduler; cron is assumed in comments | `cli/app.py:535`, `storage/settings_store.py:336`, `models/bars.py:225` |
| `sync_run_items` keeps `error` text, not the `ErrorKind` | `models/sync.py:103-149`, `core/errors.py:48-53` |
| Six in-process caches, none counting hits or misses | `datasets/base.py:152`, `pipeline/readers.py:61`, `stream/writer.py:87`, `api/ratelimit/policy.py:56`, `api/routers/meta.py:69`, `ingest/client.py:157` |
| Advisory locks already prevent overlapping runs | `storage/db.py:14`, `stream/repository.py:30-31` |
| Four compose services, every image pinned | `docker-compose.yml` |

### Package research, 2026-09-07

Versions and dates were read from PyPI and the projects' release pages,
not from memory.

| Area | Choice | Version | Why, and what was rejected |
| --- | --- | --- | --- |
| Metrics client | `prometheus-client` | 0.26.0 | Stable, no multiprocess mode needed here. |
| FastAPI HTTP metrics | `prometheus-fastapi-instrumentator` | 8.1.0 | Compatible with the locked starlette 1.6; `starlette-exporter` lacks 3.13 metadata. |
| Scheduler | `APScheduler` | 3.11.3 | 4.0 is still alpha (a6, 2025-04) and says "should NOT be used in production". `schedule` has no misfire handling; `rocketry` is inactive. |
| Cron parsing | APScheduler's `CronTrigger` | -- | No separate parser; `croniter` (pallets-eco) and `cronsim` are fine but redundant. |
| Log rendering | `structlog` `JSONRenderer` + `dict_tracebacks` + `ProcessorFormatter` | 26.1.0 (present) | Already a dependency. `python-logging-loki` last released 2019, rejected. |
| Log shipping | Grafana Alloy `loki.source.docker` | `grafana/alloy:v1.19.2` | Promtail reached end of life 2026-03-02. The Docker Loki driver is "third-party client" support and can block the daemon on retries. |
| Traces | `opentelemetry-sdk`, OTLP gRPC exporter, instrumentation for fastapi, sqlalchemy, psycopg | 1.44.0 / 0.65b0 | Core SDK is stable. The instrumentations are PyPI "Beta"; their semantic-convention status is `migration` (stable conventions reachable through `OTEL_SEMCONV_STABILITY_OPT_IN`). `-confluent-kafka` is `development` and is not used. |
| Trace store | `grafana/tempo` | v3.0.3 | Single binary. |
| Log store | `grafana/loki` | 3.7.7 | Single binary, `schema v13` + `tsdb` required. |
| Metrics store | `prom/prometheus` | v3.14.0 | |
| Dashboards, alerts | `grafana/grafana`, file provisioning `apiVersion: 1` | 13.2.1 | |
| Postgres, Redis exporters | Alloy `prometheus.exporter.postgres` / `.redis` | (Alloy) | Two fewer containers; custom queries still supported there, while `postgres_exporter --extend.query-path` is deprecated. |
| Kafka lag | `danielqsj/kafka-exporter` | v1.9.0 | Consumer-group lag and offsets. `jmx_exporter` (broker JVM) deferred. |
| Error tracking | Loki + Grafana alerting | -- | Self-hosted Sentry needs 16 GB RAM; GlitchTip (6.2) is the fallback if log-based alerting proves insufficient. Not in scope. |
| Batch-job metrics | none pushed; see `run_metrics` | -- | Pushgateway keeps series forever, has no `up`, and is a single point of failure; the numbers live in the database anyway. |

## Decisions

1. **Metrics are pulled, and the database is the source of truth for
   anything a short-lived process knows.** Long-lived processes (API,
   `stream run`, the relays, the scheduler) serve `/metrics`. Sync
   shards write their counters into a `run_metrics` table at the end of
   the run, next to the audit they already write. One exporter, inside
   the scheduler process, turns the database into gauges. No Pushgateway,
   no multiprocess registry.

2. **Traces through OpenTelemetry, metrics not.** The OTel Prometheus
   exporter has no multiprocess story and would duplicate what
   `prometheus-client` already counts. Traces go OTLP → Alloy → Tempo,
   with the stable HTTP and database semantic conventions pinned.

3. **The scheduler runs `yfin` subprocesses.** It replaces cron, not the
   runner: sharding, advisory locks, per-proxy cache directories and exit
   codes stay exactly as they are.

4. **Job definitions are settings.** Cron expressions live in the
   settings table, group `scheduler`, editable with `yfin config set`,
   reloaded without a restart. The set of jobs is fixed in code.

5. **No `symbol` label on any metric.** Ten thousand symbols times
   forty-nine datasets is a series explosion. Symbol-level detail stays in
   the database and in Loki.

6. **The stack is optional.** Every new compose service sits under
   `profiles: [observability]`; `docker compose up -d` still brings up
   the four services it does today.

7. **Thresholds are starting values, not measurements.** Alert `for`
   windows and freshness cadences are defaults the operator tunes;
   README marks them so until `docs/measurements/observability.md`
   records real numbers.

## Scheduler

### Process

`yfin scheduler run`: one long-lived process, APScheduler 3.11
`BlockingScheduler` with a `CronTrigger` per enabled job. Each firing
spawns `yfin <command>` as a subprocess with the scheduler's environment
plus `YFIN_JOB_RUN_ID`. On SIGTERM the scheduler stops accepting
firings, waits for the running subprocess, then exits. `stream run` and
the two relays are not scheduled; they are compose services.

`yfin scheduler jobs` lists every job with its cron, enabled state and
next fire time.

### Settings, group `scheduler`

| setting | default | command |
| --- | --- | --- |
| `yf_schedule_sync` | `0 2 * * *` | `yfin sync` |
| `yf_schedule_market` | `30 1 * * *` | `yfin market sync` |
| `yf_schedule_domain` | `0 3 * * 0` | `yfin domain sync` |
| `yf_schedule_bars_maintain` | `*/30 * * * *` | `yfin bars maintain` |
| `yf_schedule_stream_reconcile` | `15 * * * *` | `yfin stream reconcile` |
| `yf_schedule_prune` | `` (off) | `yfin prune` |
| `yf_schedule_usage_flush` | `5 0 * * *` | `yfin api usage flush` |
| `yf_schedule_timezone` | `UTC` | |
| `yf_schedule_misfire_grace_seconds` | `3600` | |

An empty expression means the job is not registered. The scheduler
re-reads the group every 60 seconds and calls `reschedule_job` on any
trigger that changed. `max_instances=1`, `coalesce=True`; the runner's
advisory lock is the second line of defence.

### `scheduler_runs`

| column | type |
| --- | --- |
| `id` | `BigInteger`, identity, PK |
| `job` | `AsciiKeyType(32)`, not null |
| `scheduled_at` | `TsType`, not null |
| `started_at` | `TsType`, nullable |
| `finished_at` | `TsType`, nullable |
| `exit_code` | `SmallInteger`, nullable |
| `pid` | `Integer`, nullable |
| `error` | `Text`, nullable |

Index `(job, scheduled_at desc)`. A row is inserted before the
subprocess starts and updated when it exits; a misfired trigger is
recorded with `exit_code = NULL, error = 'misfired'`. On start-up the
scheduler closes any row with `finished_at IS NULL` as
`error = 'scheduler restarted'`; if the subprocess is still alive the
advisory lock refuses the next run, which is then recorded as failed and
therefore visible.

`sync_runs` gains a nullable `job_run_id` (no FK). `audit.open_run`
reads `YFIN_JOB_RUN_ID` from the environment and stores it, so a
scheduler run and the sync run it produced are joinable without touching
any command signature.

### Scheduler metrics

`yfin_job_last_success_timestamp{job}`, `yfin_job_last_duration_seconds{job}`,
`yfin_job_runs_total{job,result="ok"|"failed"|"misfired"}`,
`yfin_job_running{job}`, `yfin_job_next_run_timestamp{job}`. Served on
the scheduler's `/metrics` together with the exporter gauges.

## Metrics

Namespace `yfin_`, unit suffixes (`_seconds`, `_total`, `_rows`),
labels from the closed set `dataset`, `family`, `scope`, `interval`,
`reason`, `state`, `job`, `route`, `outbox`, `cache`, `result`, `kind`,
`type`, `status`. Never `symbol`.

### In-process (long-lived services)

New `core/metrics.py`: one registry, `serve_metrics(port)` starting the
`prometheus-client` HTTP server in a daemon thread. Setting
`yf_metrics_port` per service (0 = off); compose sets 9100 for the API,
9101 for the scheduler, 9102 for `stream run`, 9103 and 9104 for the two
relays. A port that cannot be bound is logged as a warning and the
process carries on.

API: `prometheus-fastapi-instrumentator` at `/metrics`, unauthenticated,
behind the same per-IP fixed window `/health/ready` uses. Custom
counters:

- `yfin_api_ratelimit_decisions_total{reason="allowed"|"rate"|"quota"}`
- `yfin_api_concurrency_rejections_total`
- `yfin_api_redis_failopen_total`
- `yfin_api_problems_total{type}` -- the thirteen problem types
- `yfin_cache_ops_total{cache="plan_limits"|"readiness",result="hit"|"miss"}`

`stream run`: `yfin_stream_messages_total`, `yfin_stream_rejects_total{reason}`,
`yfin_stream_reconnects_total`, `yfin_stream_batch_seconds` (histogram),
`yfin_stream_copy_rows_total{table}`, `yfin_cache_ops_total{cache="symbol_filter"}`.

Relays: `yfin_relay_published_total{outbox}`, `yfin_relay_failures_total{outbox}`,
`yfin_relay_pass_seconds{outbox}`, `yfin_relay_chunks_dropped_total{outbox}`.

Every service: `yfin_exceptions_total{service,type}` via
`Counter.count_exceptions` at the top-level entry points, and
`yfin_build_info{version}`.

### Short-lived jobs: `run_metrics`

| column | type |
| --- | --- |
| `run_id` | FK `sync_runs.id` cascade, PK |
| `shard_index` | `SmallInteger`, PK |
| `name` | `AsciiKeyType(48)`, PK |
| `labels` | `Text`, PK -- JSON object with sorted keys |
| `value` | `BigInteger`, not null |

In a sync process `core/metrics.py` exposes the same `Counter` API but
backs it with an in-process accumulator; `audit.finalize_run` writes the
accumulator into `run_metrics` in the audit transaction, one row per
(shard, name, labels). The audit is already a separate transaction from
the symbol write, so a metrics failure cannot roll back data.

Counters recorded:

- `yahoo_requests{dataset,outcome="ok"|"empty"|"failed"}` -- at the turn
- `errors{kind}` -- where `classify_error` is called
- `retries{kind}`
- `cache_ops{cache="symbol_ctx"|"scope_reader"|"tz_cookie",result}` --
  `SyncContext.cached`, `ScopeReader`, the yfinance cache directory hit
- `proxy_turns{result}` -- `ProxyTracker`
- `rows{table,op="attempted"|"verified"|"skipped"}` -- `apply_write`

The exporter publishes the latest run per scope as
`yfin_run_<name>{scope,...labels}` gauges, summed over shards. Dataset
modules are not touched; the counters are incremented in the runner,
the context and the writer, and `test_dataset_layer_boundary` still
holds.

### The database exporter

A daemon thread in the scheduler process runs the queries below every
`yf_exporter_interval_seconds` (default 300) and updates gauges; a
scrape reads the gauges and never touches the database. Self-health:
`yfin_exporter_query_seconds{query}` and
`yfin_exporter_last_success_timestamp`. A failing query keeps the
previous values and leaves the timestamp behind, which the
`ExporterStale` alert catches.

**Freshness.** `yfin_cells_stale{family}` and `yfin_cells_total{family}`:
active symbol × dataset cells whose latest `ok|empty|skipped` item in
`sync_run_items` is older than the family's cadence. Cadence settings,
group `monitoring`: `yf_freshness_reference=36h`, `bars=36h`,
`fundamentals=36h`, `holders=7d`, `news=36h`, `discovery=7d`,
`domains=8d`. Uses `ix_sync_run_items_symbol_dataset`. Plus
`yfin_intraday_scope_stale{interval}`: symbols in `intraday_scope`
whose newest bar is within `yf_intraday_retention_warn_days` (default 3)
of Yahoo's 29-day window.

**Correctness.** For the latest run per scope: `yfin_run_items{scope,status}`,
`yfin_run_errors{scope,kind}`, `yfin_run_rows{scope,kind="fetched"|"written"|"verified"|"skipped"}`,
`yfin_run_status{scope}` (0 ok, 1 partial, 2 failed, 3 running),
`yfin_run_started_timestamp{scope}`. `yfin_proxies{state="enabled"|"cooldown"|"disabled"}`.
`sync_run_items` gains a nullable `error_kind` column (`AsciiKeyType(16)`)
set from `ErrorKind` where the item is recorded; today only the message
text is kept and the kind would have to be parsed back out of it.

**Bars.** `yfin_bar_gaps_open{interval,reason}`,
`yfin_bar_gaps_oldest_age_seconds{interval}`,
`yfin_bar_gaps_expiring{interval}` (open, and within `warn_days` of the
retention edge), `yfin_bar_gaps_resolved_total{interval,resolved_by}`
(cumulative from the table), `yfin_bar_rescales_pending`.

**Stream and outboxes.** From `stream_connection_health`:
`yfin_stream_connections{state}`, `yfin_stream_heartbeat_age_seconds`
(max), `yfin_stream_canary_age_seconds` (max),
`yfin_stream_subscribed_symbols`, `yfin_stream_reconnects` (sum). From
`stream_sessions`: message and reject totals for the open session. For
each outbox, via the existing `relay_lag` query:
`yfin_outbox_unpublished_rows{outbox}`, `yfin_outbox_oldest_age_seconds{outbox}`.

**API usage.** From `api_usage_daily`: `yfin_api_usage_requests{family}`
for today, `yfin_api_usage_estimated_days` (days flagged as
reconstructed).

### Infrastructure exporters

Alloy `prometheus.exporter.postgres` (default collectors, read-only
`yfin_monitor` role created by a migration, password from `.env`) and
`prometheus.exporter.redis`; `kafka-exporter` as its own container for
consumer-group lag. No TimescaleDB-specific queries and no `jmx_exporter`
in this iteration.

## Logs

`configure_logging(level, fmt)` with `fmt` `"console"` (default on a
TTY) or `"json"` (default otherwise; `YFIN_LOG_FORMAT` overrides). The
JSON chain: `merge_contextvars` → `redact_secrets` → `redact_credentials`
→ `add_log_level` → `add_logger_name` → `TimeStamper(iso, utc)` →
`dict_tracebacks` → `_add_trace_context` → `JSONRenderer`. Fixed fields:
`timestamp`, `level`, `logger`, `event`, `service`
(`api|sync|stream|relay|scheduler`), and when present `run_id`, `shard`,
`proxy`, `request_id`, `trace_id`, `span_id`. yfinance's stdlib records
go through `ProcessorFormatter` into the same shape; redaction applies
on both paths and a test asserts it. Everything writes to stdout.

Alloy: `discovery.docker` → `loki.source.docker` → `loki.process` (a
`json` stage promoting only `level`, `service` and `event` to labels;
`run_id` and `request_id` become structured metadata) → `loki.write`.
Loki single binary, `schema v13` + `tsdb`, 14-day retention.

Errors: no tracking service. `yfin_exceptions_total` counts them, the
`ErrorLogs` alert fires on `level=error`, and `dict_tracebacks` makes the
traceback readable in Grafana.

## Traces

New `core/tracing.py`: `configure_tracing(service)` sets up the OTel
SDK with `OTEL_EXPORTER_OTLP_ENDPOINT` (Alloy, gRPC 4317); an empty
endpoint installs a no-op provider. `OTEL_SEMCONV_STABILITY_OPT_IN=http,database`
is set by `configure_tracing`, not left to the operator, so the
attribute names the dashboards query cannot drift with a package
upgrade. Instrumentation: fastapi, sqlalchemy, psycopg. Manual spans:

| span | where | attributes |
| --- | --- | --- |
| `sync.symbol` | around `persist_symbol` | `symbol`, `dataset_count`, `rows_written` |
| `sync.dataset.fetch` | worker thread, per dataset | `dataset`, `outcome` |
| `relay.pass` | `OutboxRelay.publish_once` | `outbox`, `messages` |
| `scheduler.job` | around the subprocess | `job`, `exit_code` |

Sampling: `parentbased_traceidratio` 0.1 in the API, 1.0 in the
pipeline. Alloy `otelcol.receiver.otlp` → `otelcol.exporter.otlp` →
Tempo, 7-day retention.

Correlation: `_add_trace_context` writes the active span's ids into
every log line; Grafana's Loki datasource has a `derivedFields` entry
linking `trace_id` to Tempo, and the Tempo datasource's `tracesToLogs`
links back by `service` and time range. The API also sets `request_id`
as a span attribute.

The OTel packages live in a new `[otel]` extra. Without it
`configure_tracing` is a no-op and imports nothing that is missing --
the same pattern as the `kafka` extra, and pinned by the same kind of
test.

## Compose and provisioning

All under `profiles: [observability]`:

| service | image | note |
| --- | --- | --- |
| `scheduler` | local Dockerfile, command `yfin scheduler run` | `/metrics` 9101 |
| `prometheus` | `prom/prometheus:v3.14.0` | retention 30d, scrape 15s |
| `grafana` | `grafana/grafana:13.2.1` | provisioning mounted; anonymous access off; admin password from `.env` |
| `loki` | `grafana/loki:3.7.7` | single binary |
| `tempo` | `grafana/tempo:v3.0.3` | single binary |
| `alloy` | `grafana/alloy:v1.19.2` | `docker.sock` read-only; OTLP 4317; postgres and redis exporters |
| `kafka-exporter` | `danielqsj/kafka-exporter:v1.9.0` | `kafka:9092` |

Files under `deploy/observability/`:

```
prometheus/prometheus.yml
prometheus/rules/*.yml
alloy/config.alloy
loki/loki.yml
tempo/tempo.yml
grafana/provisioning/datasources/datasources.yml
grafana/provisioning/dashboards/dashboards.yml
grafana/provisioning/alerting/rules.yml
grafana/provisioning/alerting/contact-points.yml
grafana/dashboards/*.json
```

Dashboards (JSON, fixed `uid`, `id: null`, UI edits disabled -- the
repository is the source):

1. **Overview** -- service up/down, last run status per scope, stale
   cell ratio, open gaps, outbox lag, error rate.
2. **Sync** -- run timeline, item status distribution, error kinds,
   proxy states, per-dataset duration, cache hit ratio, Yahoo
   requests and failures.
3. **Freshness** -- stale/total per family, intraday window warnings,
   scheduler jobs with last success and lateness.
4. **Bars** -- open gaps by interval and reason, expiring gaps,
   resolutions by `resolved_by`, rescales.
5. **Stream & Relay** -- connection states, heartbeat and canary age,
   reject rate, both outbox lags, Kafka consumer lag.
6. **API** -- requests and latency, problem types, rate and quota
   decisions, Redis fail-open, usage per family, cache hits.
7. **Infra** -- Postgres connections and locks, Redis memory, Kafka
   broker.

Alerts (Grafana unified alerting, provisioned from file; `for` windows
are starting values):

| alert | expression |
| --- | --- |
| `JobOverdue` | `time() - yfin_job_last_success_timestamp > 2 × cadence` per job |
| `JobFailed` | `increase(yfin_job_runs_total{result="failed"}[1h]) > 0` |
| `SyncPartial` | `yfin_run_status{scope="symbols"} >= 1` for 30m |
| `StaleCells` | `yfin_cells_stale / yfin_cells_total > 0.05` for 1h, per family |
| `IntradayExpiring` | `yfin_bar_gaps_expiring > 0` |
| `StreamStale` | `yfin_stream_heartbeat_age_seconds > 120` or no connected connection, inside a fixed UTC market window |
| `OutboxLag` | `yfin_outbox_oldest_age_seconds > 300` |
| `RelayFailures` | `increase(yfin_relay_failures_total[15m]) > 0` |
| `ApiErrorRate` | 5xx ratio > 1 % for 10m |
| `ApiRedisFailOpen` | `increase(yfin_api_redis_failopen_total[15m]) > 0` |
| `ErrorLogs` | Loki `count_over_time({level="error"}[15m]) > 0` per service |
| `ExporterStale` | `time() - yfin_exporter_last_success_timestamp > 900` |

Contact point: Grafana UI notifications by default; when
`GRAFANA_ALERT_WEBHOOK` is set in `.env` a webhook contact point is
provisioned. No SMTP.

## Error handling

- Observability never blocks the work: a metrics port that cannot bind
  is a warning; `run_metrics` is written in the audit transaction, not
  the data transaction; the OTLP exporter drops on backlog
  (`BatchSpanProcessor`, 5-second export timeout); Alloy resumes from
  its positions file after a restart.
- A failing exporter query keeps the previous gauges and stalls the
  success timestamp.
- The scheduler closes orphaned `scheduler_runs` rows on start-up; an
  orphaned subprocess still holding the advisory lock makes the next
  run fail visibly rather than silently overlap.

## Tests

Unit:

- JSON log lines carry the fixed fields; redaction holds in both
  formats; `_add_trace_context` adds nothing without a span.
- `configure_tracing` is a no-op without the `[otel]` extra.
- The run-metrics accumulator: label ordering, shard summation, the
  `run_metrics` rows it produces.
- Scheduler: triggers built from settings, empty cron → no job, misfire
  recorded, reload reschedules a changed trigger.
- Exporter queries compile; freshness arithmetic on a synthetic
  `sync_run_items`.
- Dashboard JSON: unique `uid`, `id: null`; alert rules parse;
  `promtool check rules` and `alloy fmt --check` in CI. No Docker in
  CI.

Repo:

- Migrations: `scheduler_runs`, `run_metrics`, `sync_runs.job_run_id`,
  `sync_run_items.error_kind`, the `yfin_monitor` role;
  `alembic check` empty.
- Freshness, gap and stream queries against seeded rows.
- `YFIN_JOB_RUN_ID` lands in `sync_runs.job_run_id`.

Local integration (not CI): `--profile observability` up, one
`yfin sync --symbols AAPL`, the run visible on Overview; the queries and
their output go into `docs/measurements/observability.md`.

## Measurements to record

Into `docs/measurements/observability.md` (new):

- Freshness query duration on a synthetic 10,000 × 49 `sync_run_items`;
  acceptance 5 s at a 300 s interval. Above that, a materialised
  watermark table is the next design, not this one.
- JSON versus console logging: per-line cost and effect on a full sync.
- Tracing overhead on `persist_symbol` at sampling 1.0.
- The metrics endpoint's effect on `stream run`'s write ceiling
  (against `websocket.md`).

Until these exist, README marks every threshold and cadence as "starting
value, not measured".

## Files

New:

- `src/yfin/core/metrics.py`, `src/yfin/core/tracing.py`
- `src/yfin/scheduler/{__init__,service,jobs,exporter,queries}.py`
- `src/yfin/cli/scheduler.py` -- `yfin scheduler run|jobs`
- `src/yfin/models/ops.py` -- `scheduler_runs`, `run_metrics`
- `migrations/versions/<ts>_observability.py`
- `deploy/observability/**`, dashboards JSON
- `docs/measurements/observability.md`
- tests listed above

Changed:

- `core/logging_setup.py` -- JSON format, trace context processor
- `core/config.py` -- groups `scheduler`, `monitoring`; `yf_metrics_port`, `yf_exporter_interval_seconds`
- `pipeline/audit.py` -- `job_run_id`, `error_kind`, `run_metrics` write
- `pipeline/runner.py`, `pipeline/turn.py`, `datasets/base.py` (`SyncContext.cached`), `pipeline/readers.py`, `pipeline/contracts.py` (`ProxyTracker`) -- counter increments
- `pipeline/persist.py` -- `sync.symbol` span
- `api/app.py`, `api/ratelimit/*`, `api/routers/meta.py` -- instrumentator, counters
- `stream/runner.py`, `stream/writer.py`, `stream/relay.py` (or `outbox/relay.py`) -- counters, spans, metrics port
- `models/sync.py` -- two columns
- `docker-compose.yml`, `Dockerfile`, `.env.example`, `README.md`, `pyproject.toml`

## Implementation order

1. `core/metrics.py` with the accumulator and the served registry; unit
   tests. No consumers yet.
2. Migration: `scheduler_runs`, `run_metrics`, `job_run_id`,
   `error_kind`, monitor role.
3. Scheduler service, CLI, `scheduler_runs` lifecycle, job metrics.
4. Run-metrics counters in runner, context, writer, tracker; audit
   write.
5. Exporter queries and gauges; the freshness measurement.
6. JSON logging and the trace-context processor.
7. API instrumentator and counters; stream and relay counters.
8. Tracing module, `[otel]` extra, manual spans.
9. Compose profile, Alloy/Loki/Tempo/Prometheus configs, Grafana
   provisioning, dashboards, alerts, CI checks.
10. Local integration run; remaining measurements; README.

## Out of scope

- GlitchTip or Sentry; SMTP notifications.
- `jmx_exporter`; TimescaleDB-specific Postgres queries.
- Kubernetes manifests.
- Multi-tenant dashboards for the hosted service.
- Any `symbol`-labelled metric; a materialised freshness table.
- APScheduler 4.
