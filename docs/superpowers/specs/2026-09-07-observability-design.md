# Observability: scheduler, metrics, logs, traces and dashboards

Status: approved, not yet implemented
Date: 2026-09-07
Revised 2026-09-07 after independent review (see "Revisions" at the end).
Sibling of `2026-09-07-pipeline-change-events-design.md` ("the changes
design"). The dependencies between the two are listed under
"Implementation order"; everything else here stands on its own.

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
| structlog with `ConsoleRenderer` on **stderr**, `PrintLoggerFactory`; secrets and credentials redacted at the top level of the event dict | `core/logging_setup.py:14-29,49-55,78-84` |
| yfinance's stdlib logs go through a private `_ScrubbingHandler` with `propagate = False` | `core/logging_setup.py:87-113` |
| `run_id`, `shard`, `proxy` bound as contextvars, rebound in worker threads; `shard_main` calls `configure_logging(level)` | `core/logging_setup.py:116-126`, `pipeline/shard.py:133` |
| API logs one line per request with `request_id`, route template, status, duration; never the query string | `api/core/middleware.py:100-125` |
| `/health` and `/health/ready`, unauthenticated, a module-private per-IP window and a readiness cache | `api/routers/meta.py:45-88,117-149` |
| The API image runs `uvicorn --workers 4`; `HEALTHCHECK` probes `/health` on 8000; `WORKDIR /app` is root-owned and only `.venv` and `src` are `chown`ed | `Dockerfile` |
| `yf_tz_cache_dir` defaults to the relative `.cache/yfinance`, created on first request | `core/config.py:154-157`, `ingest/client.py:157-165` |
| No Prometheus, OpenTelemetry or Sentry anywhere; no `/metrics` | grep |
| No scheduler; cron is assumed in comments | `cli/app.py:536`, `storage/settings_store.py:336`, `models/bars.py:233-234` |
| Exit codes: 0 ok, 1 no symbols, 2 partial, 3 all failed, 4 lock not acquired, 5 no proxy | `pipeline/audit.py:29-34`, `cli/app.py:556-558` |
| `stream reconcile` takes the **sync** advisory lock | `cli/stream.py:389,397`, `storage/db.py:14` |
| `bars maintain` reports only; it never writes | `cli/bars.py:168-209` |
| Shards are separate processes (`spawn`); `finalize_run` runs in the parent | `pipeline/shard.py:122,280` |
| `sync_run_items` has no timestamp; age comes from `sync_runs.started_at`; index `(symbol, dataset)` only; nothing prunes the audit tables | `models/sync.py:67-149`, `pipeline/prune.py` |
| `sync_run_items` keeps `error` text; `SymbolPayload.failures` is `(dataset, message)` and the kinds are a separate flat list | `models/sync.py:103-149`, `pipeline/payload.py:24,37`, `pipeline/runner.py:140-141` |
| Bar datasets record `out_of_scope` for symbols outside `intraday_scope`; opt-in datasets never run under `all` | `pipeline/runner.py:125-130`, `datasets/registry.py:65` |
| Market items use a scope label as `symbol`; domain items carry `region` | `pipeline/market_runner.py:95`, `pipeline/audit.py:48-52` |
| Yahoo retention per interval: 1m 29 d, 5m/15m 59 d, 60m 729 d | `datasets/bars.py:43-56` (`BAR_LIMITS`) |
| Proxy state is `is_enabled` × `health ∈ unknown|healthy|cooldown|dead` | `models/proxies.py:50-54,98-99` |
| The settings table is read once; `SETTING_GROUPS` is a closed tuple enforced by a test; DB-managed fields are everything not in `ENV_ONLY_FIELDS` | `core/config.py:45-58,442,605-611`, `tests/unit/test_settings_split.py` |
| `.env.example` must document every env-only field and nothing that is not a `Settings` field | `tests/unit/test_env_example.py` |
| `usage flush` skips today's bucket | `api/ratelimit/usage.py:94-100`, `cli/api.py:199-215` |
| Rate-limit reason codes are integers 0/1/2; Redis fail-open happens in three places | `api/ratelimit/limiter.py:44-46,143-147`, `concurrency.py:46-47`, `usage.py` |
| CI runs ruff, mypy, pytest and the `openapi.json` diff; the committed document must be byte-identical to the generated one | `.github/workflows/ci.yml:30-46`, `tests/unit/test_api_contract.py` |
| Five in-process caches, none counting hits or misses; the per-symbol `SyncContext.cached` has a structurally constant hit ratio | `datasets/base.py:147-157`, `pipeline/readers.py:61`, `stream/writer.py:80`, `api/ratelimit/policy.py:56`, `api/routers/meta.py:69` |
| Four compose services (`api` is a local build), every image pinned | `docker-compose.yml` |

### Package research, 2026-09-07

Versions and dates were read from PyPI and the projects' release pages,
not from memory. Image tags are re-checked when pinned.

| Area | Choice | Version | Why, and what was rejected |
| --- | --- | --- | --- |
| Metrics client | `prometheus-client` | 0.26.0 | Stable. Multiprocess mode is needed for the API only. |
| FastAPI HTTP metrics | `prometheus-fastapi-instrumentator` | 8.1.0 | Compatible with the locked starlette 1.6; supports `PROMETHEUS_MULTIPROC_DIR`; `starlette-exporter` lacks 3.13 metadata. |
| Scheduler | `APScheduler` | 3.11.3 | 4.0 is still alpha (a6, 2025-04) and says "should NOT be used in production". `schedule` has no misfire handling; `rocketry` is inactive. |
| Cron parsing | APScheduler's `CronTrigger` | -- | `croniter` (pallets-eco) and `cronsim` are fine but redundant. |
| Log rendering | `structlog` `JSONRenderer` + `ExceptionRenderer` + `ProcessorFormatter` | 26.1.0 (present) | Already a dependency. `python-logging-loki` last released 2019, rejected. |
| Log shipping | Grafana Alloy `loki.source.docker` | `grafana/alloy:v1.19.2` | Promtail reached end of life 2026-03-02. The Docker Loki driver is "third-party client" support and can block the daemon on retries. |
| Traces | `opentelemetry-sdk`, OTLP gRPC exporter, instrumentation for fastapi and sqlalchemy | 1.44.0 / 0.65b0 | Core SDK is stable. The instrumentations are PyPI "Beta"; their semantic-convention status is `migration` (stable conventions selected through `OTEL_SEMCONV_STABILITY_OPT_IN`). `-psycopg` would nest a second span under every SQLAlchemy span and is left out; `-confluent-kafka` is `development` and is not used. |
| Trace store | `grafana/tempo` | 3.0.3 | Single binary. Tag verified without the `v` prefix at pin time. |
| Log store | `grafana/loki` | 3.7.7 | Single binary, `schema v13` + `tsdb` required. |
| Metrics store | `prom/prometheus` | v3.14.0 | |
| Dashboards, alerts | `grafana/grafana`, file provisioning `apiVersion: 1` | 13.2.1 | |
| Postgres, Redis exporters | Alloy `prometheus.exporter.postgres` / `.redis` | (Alloy) | Two fewer containers; custom queries still supported there, while `postgres_exporter --extend.query-path` is deprecated. |
| Kafka | `danielqsj/kafka-exporter` | v1.9.0 | Consumer-group lag of **external** consumers and partition offsets; yfin itself only produces. Broker JVM (`jmx_exporter`) deferred. |
| Error tracking | Loki + Grafana alerting | -- | Self-hosted Sentry needs 16 GB RAM; GlitchTip (6.2) is the fallback if log-based alerting proves insufficient. Not in scope. |
| Batch-job metrics | none pushed; see `run_metrics` | -- | Pushgateway keeps series forever, has no `up`, and is a single point of failure; the numbers live in the database anyway. |

## Decisions

1. **Metrics are pulled, and the database is the source of truth for
   anything a short-lived process knows.** Long-lived processes serve
   `/metrics`. Each sync shard writes its counters into a `run_metrics`
   table when it exits, next to the audit it already writes. One
   exporter, inside the scheduler process, turns the database into
   gauges. No Pushgateway. The API, which runs four uvicorn workers,
   uses `prometheus-client`'s multiprocess mode; every other service is
   one process with a plain registry.

2. **Traces through OpenTelemetry, metrics not.** The OTel Prometheus
   exporter has no multiprocess story and would duplicate what
   `prometheus-client` already counts. Traces go OTLP → Alloy → Tempo,
   with the stable HTTP and database semantic conventions pinned.

3. **The scheduler runs `yfin` subprocesses.** It replaces cron, not the
   runner: sharding, advisory locks, per-proxy cache directories and exit
   codes stay exactly as they are. Jobs that talk to Yahoo are
   serialised on one executor so two subprocesses never share an IP's
   rate budget.

4. **Job definitions are settings.** Cron expressions live in the
   settings table, group `scheduler`, editable with `yfin config set`,
   reloaded without a restart. The set of jobs is fixed in code.

5. **Freshness is measured against the schedule, not against a second
   set of numbers.** A cell is stale when its last good write is older
   than `yf_freshness_factor` × the interval of the job that writes it.
   There are no per-family cadence settings.

6. **No `symbol` label on any metric.** Ten thousand symbols times
   forty-nine datasets is a series explosion. Symbol-level detail stays in
   the database and in Loki.

7. **The stack is optional and invisible when off.** Every new compose
   service sits under `profiles: [observability]`; the OTLP endpoint,
   the metrics ports and the JSON log format are set only by the
   observability override, so `docker compose up -d` brings up the four
   services it does today with no exporter trying to reach a collector
   that is not there.

8. **Thresholds are starting values, not measurements.** Alert `for`
   windows, the freshness factor, sampling ratios and retention days are
   defaults the operator tunes; README marks them so until
   `docs/measurements/observability.md` records real numbers.

## Scheduler

### Process

`yfin scheduler run`: one long-lived process, APScheduler 3.11
`BlockingScheduler` with a `CronTrigger` per enabled job. Two executors:
`yahoo` (`ThreadPoolExecutor(1)`) for `sync`, `market`, `domain` and
`stream_reconcile`, which all talk to Yahoo or take the sync lock; and
`default` for `prune`, `usage_flush` and `bars_maintain`. `max_instances=1`
and `coalesce=True` per job. A firing that finds its job already
running is recorded as `skipped` through the `EVENT_JOB_MAX_INSTANCES`
listener; a missed one through `EVENT_JOB_MISSED` as `misfired`.

Each firing spawns `yfin <command>` in its own process group with the
scheduler's environment plus `YF_JOB_RUN_ID`. On SIGTERM the scheduler
stops firing, forwards SIGTERM to the running process groups, waits up
to `yf_schedule_stop_grace_seconds` (default 600), then SIGKILLs and
records `terminated`. Compose sets `init: true` and
`stop_grace_period` to the same value, so Docker's 10-second default
cannot orphan a shard that holds the sync lock.

`stream run` and the relays are not scheduled; they are compose services
(see Compose).

`yfin scheduler jobs` lists every job with its cron, executor, enabled
state and next fire time.

### Settings, group `scheduler`

All new settings are `int` or `str`, following the `_seconds` / `_days`
convention; `SETTING_GROUPS` gains `scheduler` and `monitoring`, and
`tests/unit/test_settings_split.py` is updated with them. Cron strings
are validated in `settings_store.validate_pair` with
`CronTrigger.from_crontab`, so `yfin config set` rejects a bad
expression instead of the scheduler failing at reload.

| setting | default | command | executor |
| --- | --- | --- | --- |
| `yf_schedule_sync` | `0 2 * * *` | `yfin sync` | yahoo |
| `yf_schedule_market` | `30 1 * * *` | `yfin market sync` | yahoo |
| `yf_schedule_domain` | `0 3 * * 0` | `yfin domain sync` | yahoo |
| `yf_schedule_stream_reconcile` | `15 * * * *` | `yfin stream reconcile` | yahoo |
| `yf_schedule_bars_maintain` | `0 4 1 * *` | `yfin bars maintain` | default |
| `yf_schedule_prune` | `` (off) | `yfin prune` | default |
| `yf_schedule_usage_flush` | `5 0 * * *` | `yfin api usage flush` | default |
| `yf_schedule_timezone` | `UTC` | | |
| `yf_schedule_misfire_grace_seconds` | `3600` | | |
| `yf_schedule_stop_grace_seconds` | `600` | | |

An empty expression means the job is not registered. The effective
misfire grace per job is `min(cadence / 2, yf_schedule_misfire_grace_seconds)`,
so an hourly job does not accept a firing that is fifty minutes late.

The scheduler re-reads the group every 60 seconds through
`settings_store.load_overrides(bootstrap_settings())` filtered to
`yf_schedule_*`; it does not use the process-wide `get_settings()`
singleton, which by design never re-reads. A changed cron calls
`reschedule_job`; a changed timezone rebuilds every trigger.

### `scheduler_runs`

| column | type |
| --- | --- |
| `id` | `BigInteger`, identity, PK |
| `job` | `AsciiKeyType(32)`, not null |
| `scheduled_at` | `TsType`, not null |
| `started_at` | `TsType`, nullable |
| `finished_at` | `TsType`, nullable |
| `exit_code` | `SmallInteger`, nullable |
| `result` | `AsciiKeyType(16)`, nullable |
| `pid` | `Integer`, nullable |
| `error` | `Text`, nullable |

Index `(job, scheduled_at desc)`. `result` is derived from the exit
code: 0 → `ok`, 2 → `partial`, 4 → `locked`, 1/3/5 and any other code →
`failed`; `misfired`, `skipped` and `terminated` are set by the
scheduler itself. A row is inserted before the subprocess starts and
updated when it exits. On start-up the scheduler closes any row with
`finished_at IS NULL` as `result = 'terminated', error = 'scheduler
restarted'`; if the subprocess is still alive the advisory lock makes
the next run `locked`, which is visible rather than an overlap.

`sync_runs` gains a nullable `job_run_id` (no FK). `audit.open_run`
reads `YF_JOB_RUN_ID` and stores it, so a scheduler run and the sync
run it produced are joinable without touching any command signature.

### Scheduler metrics

`yfin_job_last_success_timestamp{job}` (seeded from `scheduler_runs` at
start-up, so a restart does not fire `JobOverdue`),
`yfin_job_last_duration_seconds{job}`, `yfin_job_interval_seconds{job}`
(the cron's mean period, what the freshness and overdue rules divide
by), `yfin_job_runs_total{job,result}`, `yfin_job_running{job}`,
`yfin_job_next_run_timestamp{job}`. Served on the scheduler's
`/metrics` together with the exporter gauges.

### Retention

`yfin prune` gains `--audit-days N`: deletes `sync_runs` older than N
days (items and `run_metrics` cascade) and `scheduler_runs` older than
N days. Off unless given, like the other prune switches. Without it the
audit tables grow without bound, and the freshness query below reads
them.

## Metrics

Namespace `yfin_`, unit suffixes (`_seconds`, `_total`, `_rows`),
labels from the closed set `dataset`, `scope`, `kind`, `interval`,
`reason`, `state`, `job`, `result`, `handler`, `outbox`, `cache`,
`where`, `type`, `status`, `op`, `table`. Never `symbol`. Gauges read
from a table are named without `_total`; only in-process counters carry
it.

### In-process (long-lived services)

New `core/metrics.py`: one registry, `serve_metrics(port)` starting the
`prometheus-client` HTTP server in a daemon thread, and
`count_exception(exc)` which increments `yfin_exceptions_total{type}`
with the exception class name (the library's `count_exceptions` cannot
label by type). `METRICS_PORT` is an **env-only** `Settings` field
(default 0 = off); it cannot be a database setting because one value
would bind five services to one port. The observability override sets
9101 for the scheduler, 9102 for `stream run`, 9103 and 9104 for the
two relays. A port that cannot be bound is logged as a warning and the
process carries on. `yfin_build_info{version}` everywhere.

**API.** `prometheus-fastapi-instrumentator` with `metric_namespace="yfin"`,
`should_group_status_codes=False`, exposed on the uvicorn port with
`include_in_schema=False`, so `openapi.json` does not change;
`test_api_contract.py` gains an assertion that `/metrics` is not in
`paths`, and the openapi-finalisation design's list of routes outside
the contract gains `/metrics`. The instrumentator's default `handler`
label is the route template, which is why `handler` is in the closed
set. `PROMETHEUS_MULTIPROC_DIR` is set in the image and cleared by the
entrypoint, and the instrumentator builds a `MultiProcessCollector`
when it sees it; API metrics are therefore counters and histograms
only. `/metrics` sits behind the same per-IP fixed window
`/health/ready` uses, moved out of `meta.py` into `api/core/window.py`
so both can import it. `RequestContextMiddleware` no longer logs
`/metrics` and `/health*` requests: at a 15-second scrape that is
5,760 lines a day of nothing. Custom counters:

- `yfin_api_ratelimit_decisions_total{reason="allowed"|"rate"|"quota"}`
  -- the integer codes 0/1/2 mapped to these strings at the metering
  point
- `yfin_api_concurrency_rejections_total`
- `yfin_api_redis_failopen_total{where="limiter"|"concurrency"|"usage"}`
- `yfin_api_problems_total{type}` -- incremented in `problem_response`,
  the one place every problem passes through; the label set is
  `ALL_TYPES` from `api/core/errors.py`
- `yfin_cache_ops_total{cache="plan_limits"|"readiness",result="hit"|"miss"}`

**`stream run`.** `yfin_stream_messages_total`, `yfin_stream_rejects_total{reason}`,
`yfin_stream_reconnects_total`, `yfin_stream_batch_seconds` (histogram),
`yfin_stream_copy_rows_total{table}`, `yfin_cache_ops_total{cache="symbol_filter"}`.

**Relays.** `yfin_relay_published_total{outbox}`, `yfin_relay_failures_total{outbox}`,
`yfin_relay_pass_seconds{outbox}`, `yfin_relay_chunks_dropped_total{outbox}`.
`outbox` is the outbox table name: `stream_outbox`, and `pipeline_outbox`
once the changes design lands.

### Short-lived jobs: `run_metrics`

| column | type |
| --- | --- |
| `run_id` | FK `sync_runs.id` cascade, PK |
| `shard_index` | `SmallInteger`, PK |
| `name` | `AsciiKeyType(48)`, PK |
| `labels` | `String(255, collation="C")`, PK -- JSON object with sorted keys |
| `value` | `BigInteger`, not null |

In a sync process `core/metrics.py` exposes the same `Counter` API but
backs it with an in-process accumulator. **Each shard writes its own
rows**: `shard_main` flushes the accumulator into `run_metrics` in its
own short transaction just before the process exits, keyed by its
`shard_index`; the single-process path (`--shards 1`, `--no-proxy`) does
the same at the end of `run_sync`. `finalize_run` in the parent only
closes `sync_runs`; it cannot see a child's memory. Market and domain
runs flush at the end of `run_market_sync` / `run_domain_sync` with
`shard_index = 0`. The write is outside the symbol transactions, so a
metrics failure cannot roll back data.

Counters recorded, published by the exporter as `yfin_sync_<name>{scope,...labels}`
for the latest run per scope, summed over shards:

- `yahoo_requests{dataset,outcome="ok"|"empty"|"failed"}` -- at the turn
- `yahoo_errors{kind}` -- where `classify_error` is called
- `retries{kind}`
- `cache_ops{cache="scope_reader",result}` -- `ScopeReader`. The
  per-symbol `SyncContext.cached` is not counted: it is filled once and
  read a fixed number of times per symbol, so its ratio carries no
  signal. yfinance's own SQLite cache offers no hook and is not counted.
- `proxy_turns{result}` -- `ProxyTracker` in `pipeline/contracts.py`
- `write_rows{table,op="attempted"|"verified"|"skipped"}` -- `apply_write`;
  `attempted` is the distinct-key count the writer proposes, and keeps
  that meaning after the changes design's distinctness predicate

The `yfin_sync_` prefix is distinct from the `yfin_audit_` prefix below
so no metric name is registered twice with two label sets.

### The database exporter

A daemon thread in the scheduler process runs the queries below every
`yf_exporter_interval_seconds` (group `monitoring`, default 300) and
updates gauges; a scrape reads the gauges and never touches the
database. Self-health: `yfin_exporter_query_seconds{query}` and
`yfin_exporter_last_success_timestamp`. A failing query keeps the
previous values and leaves the timestamp behind, which the
`ExporterStale` alert catches.

**Freshness.** A *cell* is a `(symbol, dataset)` for symbol runs, a
`(scope_label, dataset)` for market runs and a `(symbol, region,
dataset)` for domain runs -- exactly the identity `sync_run_items`
already records. The query takes each cell's **latest** item (by
`run_id`), joined to `sync_runs.started_at` for its age. A cell is in
the universe when that latest item's status is not `out_of_scope`,
`not_attempted` or `unknown_symbol`; it is stale when the latest
`ok|empty|skipped` item is older than `yf_freshness_factor` (default 2)
× the writing job's interval (`sync` for symbol cells, `market`,
`domain`). Opt-in datasets that never ran are simply not in the
universe; bar datasets for symbols outside the intraday scope are
excluded by their `out_of_scope` status. Gauges:
`yfin_cells_stale{scope,dataset}` and `yfin_cells_total{scope,dataset}`
-- 61 datasets, no family mapping needed. The query needs a new index
`ix_sync_run_items_cell_run (symbol, dataset, run_id DESC)`; whether
`asof_state.fetched_at` answers the same question more cheaply for the
as-of datasets is on the measurement list.

`yfin_intraday_scope_stale{interval}`: symbols in `intraday_scope`
whose newest bar for that interval is within
`yf_intraday_retention_warn_days` (default 3) of that interval's Yahoo
limit from `BAR_LIMITS` -- 29 days for 1m, 59 for 5m/15m, 729 for 60m.

**Correctness.** From the latest run per `(scope, kind)` where `kind` is
`scheduled` (`job_run_id IS NOT NULL`) or `manual`:
`yfin_audit_items{scope,kind,status}`, `yfin_audit_errors{scope,kind,error_kind}`,
`yfin_audit_rows{scope,kind,measure="fetched"|"written"|"verified"|"skipped"}`,
`yfin_audit_status{scope,kind}` (0 ok, 1 partial, 2 failed; only for
finished runs), `yfin_audit_running{scope}` (0/1),
`yfin_audit_started_timestamp{scope,kind}`.
`yfin_proxies{state="disabled"|"unknown"|"healthy"|"cooldown"|"dead"}`
from `is_enabled` and `health`.

`sync_run_items` gains a nullable `error_kind` column
(`AsciiKeyType(16)`). `SymbolPayload.failures` becomes `(dataset,
message, kind)` so fetch failures carry their `ErrorKind`; write
failures in `persist_with_retry` and `run_turn` record `write`, a
crashed worker records `crash`, and the column is `NULL` where no kind
is known.

**Bars.** `yfin_bar_gaps_open{interval,reason}`,
`yfin_bar_gaps_oldest_age_seconds{interval}`,
`yfin_bar_gaps_expiring{interval}` (open, and within
`yf_intraday_retention_warn_days` of the interval's `BAR_LIMITS` edge),
`yfin_bar_gaps_resolved{interval,resolved_by}` (cumulative count from
the table), `yfin_bar_rescales_pending`.

**Stream and outboxes.** From `stream_connection_health`:
`yfin_stream_connections{state}`, `yfin_stream_heartbeat_age_seconds`
(max), `yfin_stream_canary_age_seconds` (max),
`yfin_stream_subscribed_symbols`, `yfin_stream_reconnects` (sum over
rows; the in-process `_total` counter is the one that survives
sessions). From `stream_sessions`: message and reject totals for the
open session. For each outbox, through `relay_lag`:
`yfin_outbox_unpublished_rows{outbox}` and
`yfin_outbox_oldest_age_seconds{outbox}`. Until the changes design
generalises `relay_lag(spec)`, the exporter calls it for `stream_outbox`
only.

**API usage.** From `api_usage_daily`, which holds days that have been
flushed: `yfin_api_usage_requests{family}` for the most recent flushed
day and `yfin_api_usage_day_timestamp` saying which day that is;
`yfin_api_usage_estimated_days` (days flagged as reconstructed). Today's
in-flight counts stay in Redis and are not exported.

### Infrastructure exporters

Alloy `prometheus.exporter.postgres` (default collectors) and
`prometheus.exporter.redis`; `kafka-exporter` as its own container for
external consumers' group lag. The read-only role is created by a new
command, `yfin db monitor-role --password-env MONITOR_DB_PASSWORD`
(idempotent: `CREATE ROLE IF NOT EXISTS` via `DO $$`, `GRANT pg_monitor`,
`GRANT SELECT` on the schema), not by an Alembic migration: a role is
cluster-wide, migrations run in parallel per-process schemas in the
repo tests, a password in a migration lands in `log_statement`, and
rotation would need a new revision. No TimescaleDB-specific queries and
no `jmx_exporter` in this iteration.

## Logs

Logging is re-plumbed onto the standard-library pipeline so both
structlog and stdlib records share one formatter:
`structlog.stdlib.LoggerFactory` and `BoundLogger`, a processor chain
ending in `ProcessorFormatter.wrap_for_formatter`, and one root
`StreamHandler(sys.stdout)` whose `ProcessorFormatter` carries the
shared processors as `foreign_pre_chain`. `_ScrubbingHandler` is
removed; the yfinance logger propagates to root and is redacted by the
same chain. `configure_logging(level, fmt, service)`; `shard_main`
passes `fmt` and `service` through. The move from stderr to stdout is
a deliberate change, made so Alloy sees one stream.

`fmt` is `console` (default on a TTY) or `json` (default otherwise);
`LOG_FORMAT` is an env-only `Settings` field. The JSON chain:
`merge_contextvars` → `redact_secrets` → `redact_credentials` →
`add_log_level` → `add_logger_name` → `TimeStamper(iso, utc)` →
`ExceptionRenderer(ExceptionDictTransformer(show_locals=False))` →
`_add_trace_context` → `JSONRenderer`. `show_locals` is off because the
default would serialise frame locals -- a proxy DSN, a signing key --
into Loki, past a redaction that only sees top-level keys; a test
raises inside a function holding a DSN local and asserts the rendered
line does not contain it. Fixed fields: `timestamp`, `level`,
`logger`, `event`, `service`, and when present `run_id`, `shard`,
`proxy`, `request_id`, `trace_id`, `span_id`. `service` is bound with
`bind_contextvars` at each entry point: `cli/app.py` (`sync`),
`api/app.py` (`api`), `cli/stream.py run` (`stream`) and `relay`
(`relay`), `cli/changes.py relay` (`relay`), the scheduler
(`scheduler`); a subprocess binds its own.

Alloy: `discovery.docker` → `loki.source.docker` → `loki.process` (a
`json` stage promoting only `level`, `service` and `event` to labels;
`run_id` and `request_id` become structured metadata) → `loki.write`.
Loki single binary, `schema v13` + `tsdb`, 14-day retention. Alloy
mounts `docker.sock` read-only; that is still host-root-equivalent
access for the Alloy container, and it is accepted here because the
stack is local and optional. A socket proxy is the mitigation if that
changes; out of scope.

Errors: no tracking service. `yfin_exceptions_total{type}` counts them,
the `ErrorLogs` alert fires on `level=error`, and the structured
traceback is readable in Grafana.

## Traces

New `core/tracing.py`: `configure_tracing(service, sample_ratio)` sets
`OTEL_SEMCONV_STABILITY_OPT_IN=http,database` **before** importing any
`opentelemetry.instrumentation` module (the value is read once at
import), then sets up the SDK with `OTEL_EXPORTER_OTLP_ENDPOINT` (Alloy,
gRPC 4317) and a `ParentBased(TraceIdRatioBased(sample_ratio))`
sampler; an empty endpoint installs a no-op provider. Instrumentation:
fastapi and sqlalchemy. Manual spans:

| span | where | attributes |
| --- | --- | --- |
| `sync.symbol` | around `persist_symbol` | `symbol`, `dataset_count`, `rows_written` |
| `sync.dataset.fetch` | worker thread, per dataset | `dataset`, `outcome` |
| `relay.pass` | `OutboxRelay.publish_once` | `outbox`, `messages` |
| `scheduler.job` | around the subprocess | `job`, `result` |

Sampling: 0.1 in the API, 1.0 in the pipeline, set by the observability
override through `OTEL_TRACES_SAMPLER_ARG`. Alloy `otelcol.receiver.otlp`
→ `otelcol.exporter.otlp` → Tempo, 7-day retention. `BatchSpanProcessor`
with a 5-second export timeout drops on backlog; the application never
waits.

Correlation: `_add_trace_context` writes the active span's ids into
every log line; Grafana's Loki datasource has a `derivedFields` entry
with a `matcherRegex` on `trace_id` (a line field, not a label) linking
to Tempo, and the Tempo datasource's `tracesToLogsV2` links back by
`service` and time range. The API also sets `request_id` as a span
attribute.

The OTel packages live in a new `[otel]` extra. Without it
`configure_tracing` is a no-op and imports nothing that is missing --
the same pattern as the `kafka` extra, pinned by the same kind of test.

## Compose and provisioning

### Image

The single `Dockerfile` serves every service. It installs
`--extra api --extra scheduler --extra kafka --extra otel` (`[scheduler]`
is new: `apscheduler`, `prometheus-client`; `prometheus-client` and
`prometheus-fastapi-instrumentator` join `[api]`), sets
`YF_TZ_CACHE_DIR=/var/cache/yfin` on a volume owned by the `yfin` user
(the relative default is under the root-owned `WORKDIR`, and a sync
subprocess would die on its first request), and sets
`PROMETHEUS_MULTIPROC_DIR=/run/yfin-metrics`, which the entrypoint
empties before `uvicorn` starts. The `HEALTHCHECK` stays in the
Dockerfile for the API; non-API services override it in compose with a
probe on their own `/metrics` port.

### Services

Base compose is unchanged apart from the image. Two profiles:

`stream` (not new work here, but the targets below need them to exist):
`stream` (`yfin stream run`), `stream-relay` (`yfin stream relay`), and,
once the changes design lands, `changes-relay`. Their metrics targets
are scraped when present; `up == 0` for them is shown on Overview and
**not** alerted, because the profile may legitimately be off.

`observability`:

| service | image | note |
| --- | --- | --- |
| `scheduler` | the project image, `yfin scheduler run` | `/metrics` 9101; `init: true`; `stop_grace_period` = `yf_schedule_stop_grace_seconds`; needs the `YFAPI_*` block for `usage flush` |
| `prometheus` | `prom/prometheus:v3.14.0` | retention 30d, scrape 15s |
| `grafana` | `grafana/grafana:13.2.1` | provisioning mounted; anonymous access off |
| `loki` | `grafana/loki:3.7.7` | single binary |
| `tempo` | `grafana/tempo:3.0.3` | single binary |
| `alloy` | `grafana/alloy:v1.19.2` | `docker.sock` read-only; OTLP 4317; postgres and redis exporters |
| `kafka-exporter` | `danielqsj/kafka-exporter:v1.9.0` | `kafka:9092` |

`docker-compose.observability.yml` is an override loaded with the
profile: it adds `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_TRACES_SAMPLER_ARG`,
`METRICS_PORT` and `LOG_FORMAT=json` to the base services, so none of
them tries to reach a collector when the profile is off. Variables that
belong only to the stack -- `GF_SECURITY_ADMIN_PASSWORD`,
`MONITOR_DB_PASSWORD`, `GRAFANA_ALERT_WEBHOOK` -- live in
`deploy/observability/.env.example`, read through `env_file`, so the
root `.env.example` keeps documenting exactly the `Settings` fields and
its test keeps passing. `METRICS_PORT` and `LOG_FORMAT` are `Settings`
fields and do appear in the root example.

### Files

Under `deploy/observability/`:

```
.env.example
prometheus/prometheus.yml
prometheus/rules/recording.yml
alloy/config.alloy
loki/loki.yml
tempo/tempo.yml
grafana/provisioning/datasources/datasources.yml
grafana/provisioning/dashboards/dashboards.yml
grafana/provisioning/alerting/rules.yml
grafana/provisioning/alerting/contact-points.yml
grafana/dashboards/*.json
```

Alert rules live only in Grafana's provisioning; `prometheus/rules`
holds recording rules only.

### Dashboards

JSON, fixed `uid`, `id: null`, UI edits disabled -- the repository is
the source.

1. **Overview** -- service up/down, last run status per scope, stale
   cell ratio, open gaps, outbox lag, error rate.
2. **Sync** -- run timeline, item status distribution, error kinds,
   proxy states, per-dataset duration, cache hit ratio, Yahoo requests
   and failures.
3. **Freshness** -- stale/total per dataset, intraday window warnings,
   scheduler jobs with last success and lateness.
4. **Bars** -- open gaps by interval and reason, expiring gaps,
   resolutions by `resolved_by`, rescales.
5. **Stream & Relay** -- connection states, heartbeat and canary age,
   reject rate, outbox lags, external consumer lag.
6. **API** -- requests and latency, problem types, rate and quota
   decisions, Redis fail-open by place, usage per family, cache hits.
7. **Infra** -- Postgres connections and locks, Redis memory, Kafka
   broker.

### Alerts

Grafana unified alerting, provisioned from file; `for` windows are
starting values.

| alert | expression |
| --- | --- |
| `JobOverdue` | `time() - yfin_job_last_success_timestamp > 2 * yfin_job_interval_seconds` |
| `JobFailed` | `increase(yfin_job_runs_total{result="failed"}[1h]) > 0` |
| `JobPartial` | `increase(yfin_job_runs_total{result="partial"}[1h]) > 0`, lower severity |
| `SyncFailed` | `yfin_audit_status{scope="symbols",kind="scheduled"} == 2` |
| `StaleCells` | `sum by (scope) (yfin_cells_stale) / sum by (scope) (yfin_cells_total) > 0.05` for 1h |
| `IntradayExpiring` | `yfin_bar_gaps_expiring > 0` |
| `StreamStale` | `yfin_stream_canary_age_seconds > 120` while `yfin_stream_connections{state="connected"} > 0` -- the canary is a 24/7 symbol, so no market window is needed |
| `StreamDown` | `yfin_stream_connections{state="connected"} == 0` and the `stream` target is `up` |
| `OutboxLag` | `yfin_outbox_oldest_age_seconds > 300` |
| `RelayFailures` | `increase(yfin_relay_failures_total[15m]) > 0` |
| `ApiErrorRate` | 5xx ratio > 1 % for 10m |
| `ApiRedisFailOpen` | `increase(yfin_api_redis_failopen_total[15m]) > 0` |
| `ErrorLogs` | Loki `count_over_time({level="error"}[15m]) > 0` per service |
| `ExporterStale` | `time() - yfin_exporter_last_success_timestamp > 900` |

Contact point: Grafana UI notifications by default; when
`GRAFANA_ALERT_WEBHOOK` is set a webhook contact point is provisioned.
No SMTP.

### CI

`promtool check config` and `alloy fmt --check` run through the pinned
images (`docker run --rm prom/prometheus:v3.14.0 promtool ...`), which
CI already has Docker for. A unit test parses every dashboard JSON
(unique `uid`, `id: null`) and every Grafana alerting YAML (required
keys, unique rule `uid`s).

## Error handling

- Observability never blocks the work: a metrics port that cannot bind
  is a warning; `run_metrics` is written outside the symbol
  transactions; the OTLP exporter drops on backlog; Alloy resumes from
  its positions file after a restart.
- A failing exporter query keeps the previous gauges and stalls the
  success timestamp.
- The scheduler closes orphaned `scheduler_runs` rows on start-up,
  forwards SIGTERM to process groups and bounds the wait; an orphaned
  subprocess still holding the advisory lock makes the next run
  `locked` rather than silently overlapping.

## Tests

Unit:

- JSON log lines carry the fixed fields; redaction holds in both
  formats and for yfinance's stdlib records; a traceback with a DSN in
  a local variable does not leak it; `_add_trace_context` adds nothing
  without a span.
- `configure_tracing` is a no-op without the `[otel]` extra.
- The run-metrics accumulator: label ordering, the rows it produces,
  the exporter's summation over shards.
- Scheduler: triggers and executors built from settings, empty cron →
  no job, cron validation in `validate_pair`, per-job misfire grace,
  exit-code → `result` mapping, missed and max-instances listeners,
  reload reschedules a changed trigger, SIGTERM forwarding with a fake
  process group.
- Exporter queries compile; freshness arithmetic and the universe rule
  on a synthetic `sync_run_items`, including `out_of_scope`, opt-in and
  market/domain cells.
- `/metrics` is not in `openapi.json`; `/metrics` and `/health*` are
  not request-logged; problem counter labels equal `ALL_TYPES`.
- Dashboard JSON and alert YAML rules; `SETTING_GROUPS` and
  `.env.example` tests updated.

Repo:

- Migration: `scheduler_runs`, `run_metrics`, `sync_runs.job_run_id`,
  `sync_run_items.error_kind`, `ix_sync_run_items_cell_run`;
  `alembic check` empty.
- `yfin db monitor-role` is idempotent and the role can `SELECT` but
  not write.
- Freshness, gap and stream queries against seeded rows.
- `YF_JOB_RUN_ID` lands in `sync_runs.job_run_id`; a shard's
  `run_metrics` rows appear after `shard_main` exits.
- `--audit-days` prunes runs, items, `run_metrics` and
  `scheduler_runs`.

Local integration (not CI): `--profile observability` up, one
`yfin sync --symbols AAPL`, the run visible on Overview. With the
changes design present, `yf_changes_enabled` must be off or the seven
topics pre-created, or `OutboxLag` fires on a stuck batch by design.
The queries and their output go into `docs/measurements/observability.md`.

## Measurements to record

Into `docs/measurements/observability.md` (new):

- Freshness query duration on a synthetic 10,000 × 49 `sync_run_items`
  with the new index; acceptance 5 s at a 300 s interval. Above that, a
  materialised watermark table is the next design, not this one.
  Alongside it, whether `asof_state.fetched_at` answers the same
  question for the as-of datasets.
- JSON versus console logging: per-line cost and effect on a full sync.
- Tracing overhead on `persist_symbol` at sampling 1.0.
- The metrics endpoint's effect on `stream run`'s write ceiling
  (against `websocket.md`).
- The first real values behind every alert threshold and the freshness
  factor, so README can stop calling them starting values.

## Files

New:

- `src/yfin/core/metrics.py`, `src/yfin/core/tracing.py`
- `src/yfin/scheduler/{__init__,service,jobs,exporter,queries}.py`
- `src/yfin/cli/scheduler.py` -- `yfin scheduler run|jobs`
- `src/yfin/api/core/window.py` -- the per-IP window shared by `/health/ready` and `/metrics`
- `src/yfin/models/ops.py` -- `scheduler_runs`, `run_metrics`
- `migrations/versions/<ts>_observability.py`
- `deploy/observability/**`, `docker-compose.observability.yml`, dashboards JSON
- `docs/measurements/observability.md`
- tests listed above

Changed:

- `core/logging_setup.py` -- stdlib pipeline, JSON format, `service`, trace-context processor, stdout
- `core/config.py` -- groups `scheduler`, `monitoring`; env-only `metrics_port`, `log_format`; `.env.example`; `tests/unit/test_settings_split.py`, `test_env_example.py`
- `storage/settings_store.py` -- cron validation
- `pipeline/audit.py` -- `job_run_id`, `error_kind`, `run_metrics` write helper
- `pipeline/payload.py` -- `failures` carry the kind
- `pipeline/shard.py`, `pipeline/runner.py`, `pipeline/market_runner.py`, `pipeline/domain_runner.py` -- accumulator flush, `fmt`/`service` to `configure_logging`
- `pipeline/turn.py`, `pipeline/readers.py`, `pipeline/contracts.py`, `pipeline/persist.py` -- counter increments, `error_kind`, `sync.symbol` span
- `pipeline/prune.py`, `cli/app.py` -- `--audit-days`; `service` binding
- `cli/app.py` (`db`) -- `monitor-role`
- `api/app.py`, `api/core/middleware.py`, `api/core/errors.py`, `api/ratelimit/*`, `api/routers/meta.py` -- instrumentator, request-log skip, counters, shared window
- `stream/runner.py`, `stream/writer.py`, `stream/relay.py` (or `outbox/relay.py`) -- counters, spans, metrics port
- `models/sync.py` -- two columns, one index
- `docker-compose.yml`, `Dockerfile`, `.env.example`, `pyproject.toml`
- `README.md` -- Architecture diagram (`scheduler/`), Layout table, Common commands, Infrastructure table (compose services, CI line), Planned table, settings and table counts, the "starting values" note
- `docs/superpowers/specs/2026-09-07-openapi-finalization-design.md` -- `/metrics` added to the routes outside the contract

## Implementation order

1. `core/metrics.py` with the accumulator and the served registry; unit
   tests. No consumers yet.
2. Migration: `scheduler_runs`, `run_metrics`, `job_run_id`,
   `error_kind`, the index; `yfin db monitor-role`; `--audit-days`.
3. Settings groups, cron validation, env-only fields, example and
   tests.
4. Scheduler service, executors, CLI, `scheduler_runs` lifecycle,
   signal handling, job metrics.
5. Run-metrics counters in runner, readers, tracker, writer; per-shard
   flush; `error_kind` through the payload.
6. Exporter queries and gauges; the freshness measurement.
7. Logging re-plumb, JSON format, `service`, the locals test.
8. API instrumentator, multiprocess dir, shared window, request-log
   skip, counters, contract test.
9. Stream and relay counters; tracing module, `[otel]` extra, manual
   spans.
10. Dockerfile, compose profiles and override, Alloy/Loki/Tempo/
    Prometheus configs, Grafana provisioning, dashboards, alerts, CI
    checks.
11. Local integration run; remaining measurements; README.

Steps 1–8 do not depend on the changes design. Step 9's `{outbox}` label
and `relay.pass` span, and step 6's `pipeline_outbox` lag gauges, take
the changes design's `OutboxSpec` and `relay_lag(spec)` (its steps 2
and 7) when present and instrument `stream_outbox` alone until then.
The changes design's `INFRASTRUCTURE_TABLES` must list `scheduler_runs`
and `run_metrics` once step 2 here has landed. Both designs edit
`pipeline/persist.py`, `pipeline/turn.py`, `pipeline/audit.py`,
`core/config.py`, `.env.example`, `docker-compose.yml` and `README.md`;
whichever lands second rebases on the other.

## Out of scope

- GlitchTip or Sentry; SMTP notifications.
- `jmx_exporter`; TimescaleDB-specific Postgres queries; a Docker
  socket proxy for Alloy.
- Kubernetes manifests.
- Multi-tenant dashboards for the hosted service.
- Any `symbol`-labelled metric; a materialised freshness table.
- APScheduler 4.

## Revisions

Changes made after the independent review of the first draft:

- **API multiprocess.** The image runs four uvicorn workers; the API now
  uses `PROMETHEUS_MULTIPROC_DIR` through the instrumentator and no
  separate port; `/metrics` is excluded from `openapi.json`.
- **Per-shard `run_metrics`.** Shards are separate processes; each
  flushes its own rows. `finalize_run` only closes the run.
- **Metric names.** `yfin_sync_*` (from `run_metrics`) and `yfin_audit_*`
  (from the audit tables) so no name carries two label sets; `_total`
  only on in-process counters.
- **Freshness.** Cadence derived from the schedule (`yf_freshness_factor`
  × job interval) instead of per-family hour settings; the cell
  universe defined by the latest item's status; labelled by `dataset`;
  market and domain cells defined; a new index.
- **Scheduler.** Two executors; exit codes mapped to `result` including
  `partial` and `locked`; per-job misfire grace; `bars maintain`
  monthly; settings reload through `load_overrides`; SIGTERM forwarding
  with a bounded wait; `scheduler_runs.result`.
- **Image and compose.** `[scheduler]` extra; cache dir on a volume;
  healthcheck override; `YFAPI_*` for `usage flush`; observability
  override file so the base stack never points at an absent collector;
  `stream` profile services named as the scrape targets; env-only
  `METRICS_PORT` / `LOG_FORMAT`; stack-only variables in
  `deploy/observability/.env.example`.
- **Monitor role** created by a CLI command, not a migration.
- **Logs.** Re-plumbed on the stdlib pipeline; `show_locals=False`;
  `service` bound at entry points; stdout.
- **Traces.** psycopg instrumentation dropped; opt-in set before import;
  sampler argument; `tracesToLogsV2`; `matcherRegex`.
- **Alerts** only in Grafana provisioning; CI checks through pinned
  images; `SyncPartial` replaced by `SyncFailed` on finished runs plus
  `JobPartial`; `StreamStale` on the canary.
- Proxy states, `BAR_LIMITS` per interval, usage gauge day, fail-open
  `where`, problem counter tied to `ALL_TYPES`, `SyncContext.cached`
  counter dropped, line references and the stderr claim corrected;
  audit retention added.
