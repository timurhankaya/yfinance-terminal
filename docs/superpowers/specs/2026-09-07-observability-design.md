# Observability: scheduler, metrics, logs, traces and dashboards

Status: implemented, 2026-09-08 (see "Revisions" for what changed while building it)
Date: 2026-09-07
Revised 2026-09-07 after two rounds of independent review (see "Revisions" at the end).
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
| `/health` and `/health/ready`, unauthenticated, a module-private `_FixedWindow` (`_limiter`) sized by `health_rate_limit_per_minute`, and a readiness cache | `api/routers/meta.py:45-88,117-153` |
| The API image runs `uvicorn --workers 4`; `HEALTHCHECK` probes `/health` on 8000; `WORKDIR /app` is root-owned and only `.venv` and `src` are `chown`ed | `Dockerfile` |
| `prometheus_client` picks its value class from `PROMETHEUS_MULTIPROC_DIR` **at import time**, process-wide | `prometheus_client/values.py` (0.26.0) |
| APScheduler 3.11 counts `max_instances` per job id; a job on a busy single-thread executor is queued, and misfire is evaluated when it is dequeued | `apscheduler/executors/base.py` (3.11.3) |
| `validate_pair` builds a `Settings` from the candidate, so pydantic validators are what reject a value | `storage/settings_store.py:293-307` |
| The OTel SDK reads `OTEL_TRACES_SAMPLER*` only when no sampler is passed in code | `opentelemetry-sdk` 1.44.0 `TracerProvider.__init__` |
| `yf_tz_cache_dir` defaults to the relative `.cache/yfinance`, created on first request | `core/config.py:154-157`, `ingest/client.py:157-165` |
| No Prometheus, OpenTelemetry or Sentry anywhere; no `/metrics` | grep |
| No scheduler; cron is assumed in comments | `cli/app.py:536`, `storage/settings_store.py:336`, `models/bars.py:233-234` |
| Exit codes: 0 ok, 1 no symbols, 2 partial, 3 all failed, 4 lock not acquired, 5 no proxy | `pipeline/audit.py:29-34`, `cli/app.py:556-558` |
| `stream reconcile` takes the **sync** advisory lock | `cli/stream.py:389,397`, `storage/db.py:14` |
| `bars maintain` reports only; it never writes | `cli/bars.py:168-209` |
| Shards are separate processes (`spawn`); `finalize_run` runs in the parent | `pipeline/shard.py:122,280` |
| `sync_run_items` has no timestamp; age comes from `sync_runs.started_at`; index `(symbol, dataset)` only; nothing prunes the audit tables | `models/sync.py:67-149`, `pipeline/prune.py` |
| `sync_run_items` keeps `error` text; `SymbolPayload.failures` is `(dataset, message)` and the kinds are a separate flat list | `models/sync.py:103-149`, `pipeline/payload.py:24,37`, `pipeline/runner.py:140-141` |
| Bar datasets record `out_of_scope` for symbols outside `intraday_scope`; opt-in datasets never run under `all`; `not_attempted` is written only for the bootstrap dataset when a shard is pulled | `pipeline/runner.py:125-130`, `datasets/registry.py:78-101`, `pipeline/audit.py:277-299` |
| Market items use a scope label as `symbol`; domain items carry `region`; a multi-table dataset writes one item per table, each with its own status | `pipeline/market_runner.py:96`, `pipeline/audit.py:48-52,55-108` |
| `run_turn` classifies fetch errors but `failed_records` cannot carry the kind | `pipeline/turn.py:88-101`, `pipeline/audit.py:109` |
| `yfin config export` and `config schema --json` print JSON to **stdout**; `scripts/dump_openapi.py` too | `cli/settings.py:273-278,292` |
| The API configures logging lazily, through `get_settings()` on the first engine | `api/storage/session.py:22`, `core/config.py:498` |
| With `LOG_LEVEL=DEBUG` yfinance installs its own unredacted `StreamHandler` if its logger has none | `ingest/client.py:138-143`, `core/logging_setup.py:88-93` |
| No `ENTRYPOINT` in the Dockerfile; `apply_write` lives in `storage/contracts.py` | `Dockerfile`, `storage/contracts.py:140` |
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
   that is not there. The override is a second compose file loaded
   with `-f`; a profile selects services, it does not select files.

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
and `coalesce=True` per job.

What the single-thread `yahoo` executor actually does, verified against
3.11.3: `max_instances` is counted per job id, so a *different* job
fired while `sync` runs is **queued**, not rejected, and its misfire
grace is evaluated when it is dequeued. So while a three-hour `sync`
runs, an hourly `stream_reconcile` waits in the queue; if it waits
longer than its grace it is dropped as `misfired`
(`EVENT_JOB_MISSED`), otherwise it runs late, and the lateness is
`started_at - scheduled_at` in `scheduler_runs`, shown on the Freshness
dashboard. A second firing of a job that is itself still running or
queued is `skipped` (`EVENT_JOB_MAX_INSTANCES`). `coalesce` only merges
firings the scheduler process itself missed, for instance across a
restart.

Each firing spawns `yfin <command>` in its own process group with the
scheduler's environment plus `YF_JOB_RUN_ID`. On SIGTERM the scheduler
stops firing, forwards SIGTERM to the running process groups, waits up
to `yf_schedule_stop_grace_seconds` (default 600), then SIGKILLs and
records `terminated`. Compose sets `init: true` and
`stop_grace_period: ${SCHEDULER_STOP_GRACE:-620s}` -- a literal, since a
compose file cannot read the settings table -- and the scheduler's own
wait must stay below it, so Docker's 10-second default cannot orphan a
shard that holds the sync lock.

`stream run` and the relays are not scheduled; they are compose services
(see Compose).

`yfin scheduler jobs` lists every job with its cron, executor, enabled
state and next fire time.

### Settings, group `scheduler`

All new settings are `int` or `str`, following the `_seconds` / `_days`
convention; `SETTING_GROUPS` gains `scheduler` and `monitoring`, and
`tests/unit/test_settings_split.py` is updated with them. Cron strings
are validated by a pydantic `field_validator` on the `yf_schedule_*`
fields, which is what `validate_pair` and the loader both go through;
the validator imports `CronTrigger.from_crontab` lazily and falls back
to a five-field syntax check when the `[scheduler]` extra is not
installed, so `core/config.py` never imports APScheduler at module
level and `yfin config set` rejects a bad expression instead of the
scheduler failing at reload.

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
(the cron's mean period: the trigger is advanced with
`get_next_fire_time` over at least 400 days and the span is divided by
the number of firings; the same value feeds the per-job misfire grace
and the freshness and overdue rules), `yfin_job_runs_total{job,result}`, `yfin_job_running{job}`,
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
process carries on. `yfin_build_info{version}` everywhere, declared as a
`Gauge` with value 1 and `multiprocess_mode="max"` -- not `Info`, which
does not work in multiprocess mode. `core/metrics.py` imports
`prometheus_client` lazily, after the environment is final, because the
library chooses its value class from `PROMETHEUS_MULTIPROC_DIR` at
import time.

**API.** `prometheus-fastapi-instrumentator`:
`Instrumentator(should_group_status_codes=False,
should_instrument_requests_inprogress=False,
excluded_handlers=["/metrics", "/health"]).instrument(app,
metric_namespace="yfin").expose(app, include_in_schema=False,
dependencies=[Depends(metrics_window)])`. `include_in_schema=False`
keeps `/metrics` out of `openapi.json`; what keeps it out of the
contract tests is the `("/v1", "/oauth", "/health")` prefix filter in
`test_api_contract.py`, which gains an explicit assertion that
`/metrics` is not in `paths`. The instrumentator's default `handler`
label is the route template, which is why `handler` is in the closed
set. `PROMETHEUS_MULTIPROC_DIR` is set **only for the `api` service** in
compose and emptied by the entrypoint before `uvicorn` starts; it must
not be an image-wide `ENV`, or every other service would switch to
multiprocess mode too. The instrumentator builds a
`MultiProcessCollector` when it sees the variable; API metrics are
therefore counters and histograms only, no in-progress gauge and no
`mark_process_dead` hook. The API never calls `serve_metrics`, and the
override does not set `METRICS_PORT` for it. `metrics_window` is the
per-IP fixed window `/health/ready` already uses
(`health_rate_limit_per_minute`), moved out of `meta.py` into
`api/core/window.py` so both can import it. `RequestContextMiddleware`
no longer logs `/metrics` and `/health*` requests: at a 15-second
scrape that is 5,760 lines a day of nothing. `create_app` calls
`configure_logging` explicitly instead of relying on the first engine
to do it, or the module-level loggers would be cached before the
format is set. Custom counters:

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
- `write_rows{table,op="attempted"|"verified"|"skipped"}` -- `apply_write` in `storage/contracts.py`;
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

**Freshness.** A *cell* is `(symbol, region, dataset)` -- exactly the
identity `sync_run_items` records, with `region` `NULL` outside domain
runs and `symbol` the scope label for market runs. A multi-table
dataset writes one item per table per run, each with its own status,
so the query first reduces a cell's items **per run** to the worst
status (`failed` beats `ok`), then takes the latest run, joined to
`sync_runs.started_at` for its age. A cell is in the universe when that
latest status is not `out_of_scope` or `unknown_symbol`;
`not_attempted` stays in the universe and is not good, so a shard that
was pulled shows as a gap instead of vanishing. A cell is stale when
its latest `ok|empty|skipped` run is older than `yf_freshness_factor`
(default 2) × the writing job's interval (`sync` for symbol cells,
`market`, `domain`). `skipped` counts as good because a content-hash
skip is a verification; the `--start/--end` date-range skip shares the
status and slightly flatters a manual ranged run, which is accepted.
Opt-in datasets that never ran are simply not in the universe; bar
datasets for symbols outside the intraday scope are excluded by their
`out_of_scope` status. Gauges: `yfin_cells_stale{scope,dataset}` and
`yfin_cells_total{scope,dataset}` -- 61 datasets, no family mapping
needed. The query needs a new index
`ix_sync_run_items_cell_run (symbol, region, dataset, run_id DESC)`;
whether `asof_state.fetched_at` answers the same question more cheaply
for the as-of datasets is on the measurement list.

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
message, kind)` so symbol-run fetch failures carry their `ErrorKind`;
`audit.failed_records` gains a `kind` parameter, which `run_turn`
fills from the `classify_error` result it already computes on the
fetch path and with `write` on the write path; `persist_with_retry`
records `write`, a crashed worker `crash`, and the column is `NULL`
where no kind is known.

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
(idempotent: `CREATE ROLE IF NOT EXISTS` via `DO $$`, `GRANT pg_monitor`
-- enough for Alloy's default collectors, and no `SELECT` on the data
tables until a custom query needs one), not by an Alembic migration: a role is
cluster-wide, migrations run in parallel per-process schemas in the
repo tests, a password in a migration lands in `log_statement`, and
rotation would need a new revision. No TimescaleDB-specific queries and
no `jmx_exporter` in this iteration.

## Logs

Logging is re-plumbed onto the standard-library pipeline so both
structlog and stdlib records share one formatter:
`structlog.stdlib.LoggerFactory` and `BoundLogger`, a processor chain
ending in `ProcessorFormatter.wrap_for_formatter`, and one root
`StreamHandler(sys.stderr)` whose `ProcessorFormatter` carries the
shared processors as `foreign_pre_chain`. Logs **stay on stderr**:
`yfin config export`, `config schema --json` and `dump_openapi.py`
print machine-readable JSON to stdout and logging is configured before
they run, so a log line on stdout would corrupt them; Alloy's
`loki.source.docker` reads both streams and labels them, so nothing is
lost. `_ScrubbingHandler` is removed; the yfinance logger keeps a
`NullHandler` -- otherwise, at `LOG_LEVEL=DEBUG`, yfinance installs its
own unredacted `StreamHandler` on a handler-less logger -- and
propagates to root, where the same chain redacts it; `ingest/client.py`
is where that bridge is called. `configure_logging(level, fmt,
service)`; `shard_main` passes `fmt` and `service` through.

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

New `core/tracing.py`: `configure_tracing(service)` sets
`OTEL_SEMCONV_STABILITY_OPT_IN=http,database` **before** importing any
`opentelemetry.instrumentation` module (the value is read once, when
the first instrumentor initialises), then sets up the SDK with
`OTEL_EXPORTER_OTLP_ENDPOINT` (Alloy, gRPC 4317) and **no sampler
argument**, so the SDK honours `OTEL_TRACES_SAMPLER` and
`OTEL_TRACES_SAMPLER_ARG` from the environment -- a sampler built in
code would silently override them. An empty endpoint installs a no-op
provider. Instrumentation: fastapi and sqlalchemy (which recognises the
psycopg 3 engine through `engine.name == "postgresql"`). Manual spans:

| span | where | attributes |
| --- | --- | --- |
| `sync.symbol` | around `persist_symbol` | `symbol`, `dataset_count`, `rows_written` |
| `sync.dataset.fetch` | worker thread, per dataset | `dataset`, `outcome` |
| `relay.pass` | `OutboxRelay.publish_once` | `outbox`, `messages` |
| `scheduler.job` | around the subprocess | `job`, `result` |

Sampling: the override sets `OTEL_TRACES_SAMPLER=parentbased_traceidratio`
everywhere and `OTEL_TRACES_SAMPLER_ARG` to 0.1 for the API and 1.0 for
the pipeline services. Alloy `otelcol.receiver.otlp`
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
subprocess would die on its first request), and adds a
`docker/entrypoint.sh` (there is none today) that, when the command is
the API, empties `PROMETHEUS_MULTIPROC_DIR` before `exec`ing `uvicorn`.
The variable itself comes from the `api` service's compose environment,
never from an image `ENV`. The `HEALTHCHECK` stays in the Dockerfile
for the API; non-API services override it in compose with a probe on
their own `/metrics` port.

### Services

The base file gains one profile and is otherwise unchanged apart from
the image. Two profiles:

`stream`, **added by this design** in `docker-compose.yml`: `stream`
(`yfin stream run`) and `stream-relay` (`yfin stream relay`), both on
the project image with the `YF_*` block; the changes design adds
`changes-relay` next to them. Their metrics targets are scraped when
present; `up == 0` for them is shown on Overview and **not** alerted,
because the profile may legitimately be off.

`observability`:

| service | image | note |
| --- | --- | --- |
| `scheduler` | the project image, `yfin scheduler run` | `/metrics` 9101; `init: true`; `stop_grace_period: ${SCHEDULER_STOP_GRACE:-620s}`; needs the `YFAPI_*` block for `usage flush` |
| `prometheus` | `prom/prometheus:v3.14.0` | retention 30d, scrape 15s |
| `grafana` | `grafana/grafana:13.2.1` | provisioning mounted; anonymous access off |
| `loki` | `grafana/loki:3.7.7` | single binary |
| `tempo` | `grafana/tempo:3.0.3` | single binary |
| `alloy` | `grafana/alloy:v1.19.2` | `docker.sock` read-only; OTLP 4317; postgres and redis exporters |
| `kafka-exporter` | `danielqsj/kafka-exporter:v1.9.0` | `kafka:9092` |

`docker-compose.observability.yml` is a second compose file, loaded
explicitly:

```
docker compose -f docker-compose.yml -f docker-compose.observability.yml \
  --profile observability up -d
```

It adds `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_TRACES_SAMPLER`,
`OTEL_TRACES_SAMPLER_ARG`, `METRICS_PORT` and `LOG_FORMAT=json` to the
base services and `PROMETHEUS_MULTIPROC_DIR` to `api`, so none of them
tries to reach a collector when the file is not loaded. Variables that
belong only to the stack -- `GF_SECURITY_ADMIN_PASSWORD`,
`MONITOR_DB_PASSWORD`, `GRAFANA_ALERT_WEBHOOK`, `SCHEDULER_STOP_GRACE`
-- live in `deploy/observability/.env` (copied from the committed
`.env.example` next to it) and reach the containers through
`env_file`. `env_file` does not feed compose's own `${...}`
interpolation, so the compose files never reference these names with
`${}` except `SCHEDULER_STOP_GRACE`, which is read from the shell
environment or the root `.env`: Grafana reads `GF_*` from its
environment, the alerting provisioning file reads
`$GRAFANA_ALERT_WEBHOOK` at Grafana's load time, and Alloy reads
`sys.env("MONITOR_DB_PASSWORD")`. The root `.env.example` keeps
documenting exactly the `Settings` fields and its test keeps passing;
`METRICS_PORT` and `LOG_FORMAT` are `Settings` fields and do appear
there, and `SNAPSHOT_ENV_ONLY` and the "eight env-only fields" notes in
`cli/settings.py` and `test_env_example.py` become ten.

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
  not request-logged; problem counter labels equal `ALL_TYPES`; the
  cron `field_validator` rejects a bad expression with and without
  APScheduler installed.
- Dashboard JSON and alert YAML rules; `SETTING_GROUPS` and
  `.env.example` tests updated.

Repo:

- Migration: `scheduler_runs`, `run_metrics`, `sync_runs.job_run_id`,
  `sync_run_items.error_kind`, `ix_sync_run_items_cell_run`;
  `alembic check` empty.
- `yfin db monitor-role` is idempotent; the role can read `pg_stat_*`
  views and cannot read or write data tables.
- Freshness, gap and stream queries against seeded rows, including a
  multi-table dataset with one failed table, a `not_attempted` cell and
  a domain cell with two regions.
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
- `docker/entrypoint.sh`
- `src/yfin/models/ops.py` -- `scheduler_runs`, `run_metrics`
- `migrations/versions/<ts>_observability.py`
- `deploy/observability/**`, `docker-compose.observability.yml`, dashboards JSON
- `docs/measurements/observability.md`
- tests listed above

Changed:

- `core/logging_setup.py` -- stdlib pipeline, JSON format, `service`, trace-context processor
- `ingest/client.py` -- the yfinance logger bridge (`NullHandler`, propagate)
- `cli/settings.py` -- env-only count note
- `core/config.py` -- groups `scheduler`, `monitoring`; env-only `metrics_port`, `log_format`; the cron `field_validator`; `.env.example`; `tests/unit/test_settings_split.py`, `test_env_example.py`
- `pipeline/audit.py` -- `job_run_id`, `error_kind`, `failed_records(kind=...)`, `run_metrics` write helper
- `pipeline/payload.py` -- `failures` carry the kind
- `pipeline/shard.py`, `pipeline/runner.py`, `pipeline/market_runner.py`, `pipeline/domain_runner.py` -- accumulator flush, `fmt`/`service` to `configure_logging`
- `pipeline/turn.py`, `pipeline/readers.py`, `pipeline/contracts.py`, `storage/contracts.py`, `pipeline/persist.py` -- counter increments, `error_kind`, `sync.symbol` span
- `pipeline/prune.py`, `cli/app.py` -- `--audit-days`; `service` binding
- `cli/app.py` (`db`) -- `monitor-role`
- `api/app.py`, `api/core/middleware.py`, `api/core/errors.py`, `api/ratelimit/*`, `api/routers/meta.py` -- instrumentator, explicit `configure_logging`, request-log skip, counters, shared window
- `tests/unit/test_api_contract.py` -- `/metrics` assertion
- `stream/runner.py`, `stream/writer.py`, `stream/relay.py` (or `outbox/relay.py`) -- counters, spans, metrics port
- `models/sync.py` -- two columns, one index
- `docker-compose.yml` (`stream` profile, image), `Dockerfile`, `.env.example`, `pyproject.toml`
- `docs/measurements/README.md` -- index row for `observability.md`
- `README.md` -- Architecture diagram (`scheduler/`), Layout table, Common commands (the two-file compose invocation), Infrastructure table (compose services, CI line), Planned table, settings and table counts, the "starting values" note

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
  `service` bound at entry points.
- **Traces.** psycopg instrumentation dropped; opt-in set before import;
  sampler argument; `tracesToLogsV2`; `matcherRegex`.
- **Alerts** only in Grafana provisioning; CI checks through pinned
  images; `SyncPartial` replaced by `SyncFailed` on finished runs plus
  `JobPartial`; `StreamStale` on the canary.
- Proxy states, `BAR_LIMITS` per interval, usage gauge day, fail-open
  `where`, problem counter tied to `ALL_TYPES`, `SyncContext.cached`
  counter dropped, line references corrected; audit retention added.

After the second review:

- **`PROMETHEUS_MULTIPROC_DIR` only on the `api` service**, never an
  image `ENV`; `prometheus_client` imported lazily; `yfin_build_info` as
  a `Gauge`, not `Info`.
- **Logs stay on stderr**: stdout carries machine-readable output for
  `config export`, `config schema --json` and `dump_openapi.py`.
- **Executor semantics** stated as verified: queued, misfire on dequeue,
  lateness recorded, `skipped` per job id.
- **Cron validation** as a pydantic `field_validator` with a lazy
  APScheduler import; `stop_grace_period` a compose literal above the
  scheduler's own wait.
- **Compose** as a second `-f` file; `env_file` semantics and the
  `.env` copy stated; the `stream` profile added here.
- **Sampler from the environment**; the semconv variable read at the
  first instrumentor.
- **Freshness cell** `(symbol, region, dataset)` with per-run worst
  status; `not_attempted` kept in the universe; index widened.
- `failed_records(kind=...)`, yfinance `NullHandler`, explicit
  `configure_logging` in `create_app`, instrumentator parameters and
  the prefix-filter fact, entrypoint file, grant narrowed, interval
  computation, `apply_write` location, env-only count, measurements
  index, line references.

While implementing step 6 (the exporter):

- **The freshness statement is one aggregation, not two `DISTINCT ON`
  CTEs joined together.** The literal shape in "The database exporter"
  above was implemented first and measured at 4.24 s median on the
  synthetic 10,000 × 49 table; the join could only merge on `symbol`
  (`region IS NOT DISTINCT FROM` is not mergeable) and discarded 23.0 of
  23.5 million rows in a join filter. Replacing it with a single
  `GROUP BY (symbol, region, dataset)` carrying
  `(array_agg(worst ORDER BY run_id DESC))[1]` and
  `MAX(started_at) FILTER (WHERE worst IN good)` gives the same numbers --
  the 65 repo cases passed unchanged across the swap -- at **2.35 s**.
  `docs/measurements/observability.md`.
- **The acceptance criterion holds only with `--audit-days` set.** The
  cost scales with `sync_run_items` rows, not with cells: three nights of
  history is 2.35 s, seven is 4.89 s and fails the 5 s criterion. Audit
  retention is therefore a requirement of running the exporter, not an
  option, and the README note has to say so.
- **`ix_sync_run_items_cell_run` is not used by the freshness query.** It
  reads every cell, so the planner scans sequentially. Measured, recorded,
  and deliberately not acted on: the index still serves a single-cell
  "why is this symbol stale" lookup, and dropping it is a migration.
- **`asof_state` is the right shape and the wrong contents.** The same
  question over it costs 0.048 s -- forty-nine times faster -- but it
  carries no status and no region, so neither the universe rule nor a
  domain cell can be expressed. If freshness outgrows its budget the next
  design is a watermark table carrying both, and the measurement is the
  evidence it would run in milliseconds.
- **The exporter republishes the shard counters without `_total`.**
  `yfin_sync_yahoo_requests_total` in `run_metrics` becomes the gauge
  `yfin_sync_yahoo_requests{scope,dataset,outcome}`, which is what the
  `yfin_sync_<name>{scope,...labels}` line above already spelled. The
  suffix has to go: the value is the latest run's, not a monotonic total
  of the scheduler process, and keeping it would register one name with
  two label sets. Derived from the counter declarations rather than
  listed, so a new counter is exported with no second edit.
  `yfin_cells_total` keeps its `_total` -- there it is the denominator of
  `yfin_cells_stale`, not the counter suffix.
- **Three labels join the closed set**: `error_kind` (because
  `yfin_audit_errors` already spends `kind` on the scheduled/manual
  split), `query` (the exporter's self-health) and `version`. `version`
  makes the code match this document's own `yfin_build_info{version}`;
  step 1 had declared it with `type`.
- **Both outboxes are exported.** The changes design has landed, so
  `relay_lag(spec)` is generalised and the exporter reports
  `stream_outbox` and `pipeline_outbox` rather than the first alone.
- **`yfin_job_runs_total` is a real counter**, incremented in the
  scheduler where the result is decided, including for the `misfired` and
  `skipped` firings that never become a subprocess. The other five job
  metrics are gauges the exporter reads from `SchedulerService` through a
  callable -- `job_samples()` -- rather than from a table, and a job that
  has never run or never succeeded reports NO timestamp rather than 0,
  which would read as the epoch and fire `JobOverdue` on the day a job is
  added.
- **A query owns the gauges it clears.** Clearing happens only after the
  query returned, so a failure leaves the previous refresh standing; and
  it is per query rather than global, so a failing query cannot wipe
  numbers another one filled in the same pass. A test asserts no two
  queries own the same gauge.

While implementing step 7 (logging):

- **`service` is a processor, not a contextvar.** The design says it is
  bound with `bind_contextvars` at each entry point. It cannot be:
  `ThreadPoolExecutor` does not copy the context into its workers -- which
  is why `bind_shard_context` exists and is called again in every thread --
  so a `service` bound that way would be missing from exactly the fetch and
  normalise lines a dashboard filters by service to find. It is module
  state written by `configure_logging(service=...)` and added by
  `_add_service`, which puts it on every line including foreign records and
  worker threads. A test submits a log call to a pool and asserts the field
  survives.
- **`cache_logger_on_first_use=False`.** A cached logger keeps the chain it
  was built with, and `configure_logging` is deliberately called more than
  once per process -- by a CLI command, then by `create_app`, then by a
  shard once it has read its settings. The design worked around this by
  requiring `create_app` to configure first; the explicit call is still
  there, for the uvicorn access log rather than for the cache.
- **Exception rendering lives in the formatter, not the shared chain.** The
  two renderers want it in different shapes -- `ConsoleRenderer` formats
  `exc_info` itself, `JSONRenderer` needs it already turned into data -- and
  `ProcessorFormatter` has moved `record.exc_info` into the event dict by
  then, so a stdlib record's traceback is still rendered by the same code
  as a structlog one's. `show_locals=False` on BOTH sides rather than only
  the JSON one: a console traceback on a terminal is one `2>` away from a
  file. Two tests raise inside a function holding a DSN local and assert
  neither rendering leaks it.
- **The `[otel]` extra lands here rather than in step 9.**
  `_add_trace_context` is step 7's, and it has to type-check; the extra is
  also the repo's existing answer to an optional dependency (the `kafka`
  pattern), where the alternative would have been a mypy override claiming
  a package with `py.typed` has none. `configure_tracing` and the manual
  spans stay in step 9.
- **`_add_trace_context` imports the FUNCTION**, `from
  opentelemetry.trace import get_current_span`, not the module:
  `opentelemetry` is a namespace package and importing `trace` from it
  leaves mypy resolving the name against the namespace rather than against
  `opentelemetry-api`. The miss is latched in a module flag so a process
  without the extra pays one `ImportError`, not one per line.
- **A missing span adds nothing, not zeros.** An all-zero id is what
  OpenTelemetry returns for the invalid span, and Grafana's
  `derivedFields` would turn it into a link to a trace that does not exist.

While implementing step 8 (the API):

- **The instrumentator is built by `build_instrumentator(registry=None)`,
  separate from installing it.** Not decoration: on a duplicate metric
  registration `prometheus-fastapi-instrumentator` returns `None` from its
  metric factory and attaches NO instrumentation, so the SECOND app built
  in one process serves a `/metrics` that never moves. Production has one
  app per process and is unaffected; a test suite builds dozens, and
  against the default registry every assertion about the series would pass
  or fail on collection order. The split lets a test point the same four
  parameters at its own registry and actually observe what they do.
- **Two windows, not one.** `api/core/window.py` keeps a FixedWindow PER
  ENDPOINT NAME rather than one shared instance. A Prometheus scraping
  every fifteen seconds is four requests a minute; sharing a bucket with
  `/health/ready` would let the scrape spend a Kubernetes probe's
  allowance, and the two failures would be indistinguishable.
- **`yfin_api_problems_total` is incremented BEFORE the token-endpoint
  branch** in `problem_response`. An error that leaves in the RFC 6749
  shape is still an error the dashboard has to see, and counting after the
  branch would make `/oauth/token` the one path whose failures are
  invisible.
- **The fail-open counter is incremented alongside the decision, not
  instead of it.** A request the limiter let through because Redis was
  gone is counted as `reason="allowed"` AND as
  `where="limiter"`: it really was allowed, and the second counter is what
  says the first one cannot be trusted for that minute.
- **`_is_noise` skips `/health` by prefix and `/metrics` exactly.** The
  request-log skip is a prefix match on `/health/` plus two literals rather
  than a blanket `startswith`, so a future `/healthcheck-report` would be
  logged like any other route.
- **`prometheus-fastapi-instrumentator` is in the `[api]` extra**, floored
  at 8.1.0 rather than pinned: unlike `prometheus-client`, it does not
  decide anything at import time, and the thing worth pinning exactly is
  the library whose value class the whole process inherits.

While implementing step 9 (stream, relays, tracing):

- **`metrics.timed(name, **labels)`** joins `inc` and `set_gauge`: a
  context manager that records into a histogram and records EVEN WHEN THE
  BLOCK RAISES. A pass that failed is still a pass that took time, and
  dropping its duration would flatten the histogram exactly when something
  is going wrong. There is no accumulator branch -- `run_metrics` stores
  integers keyed by name and labels, which is a counter's shape and not a
  histogram's, and a shard's durations already go to
  `sync_run_items.duration_ms`.
- **`yfin_stream_rejects_total` is incremented BEFORE the sampler.**
  `RejectSampler` caps how many rows one `(symbol, reason)` pair may write,
  so `stream_rejects` deliberately under-reports a storm. Counting after it
  would make the metric agree with the table and both be wrong; the metric
  is the number that is not sampled.
- **`yfin_cache_ops_total{cache="symbol_filter"}` counts SYMBOLS, not
  calls.** A batch of 500 ticks holding one unknown symbol is 499 hits and
  one miss; counting calls would report that batch as a 100 % miss and the
  ratio would be unreadable.
- **An empty relay pass is not timed and draws no span.** At the idle poll
  rate empty passes would be most of the histogram and would pull the
  median to zero on exactly the graph that answers "is the relay keeping
  up".
- **`scheduler.job` sets `result` INSIDE the span.** A span that has ended
  takes no further attributes, and `result` is the one thing anybody would
  filter these traces by -- so the exit-code mapping moved inside the
  `with` block. The subprocess is deliberately not a child of this span:
  no context crosses the fork, and pretending otherwise would draw a trace
  the collector never receives.
- **`sync.dataset.fetch` covers fetch AND normalize.** They are not
  separable from the outside, and both run on the worker thread no
  automatic instrumentation reaches -- yfinance talks through `curl_cffi`,
  which has no OTel instrumentation at all.
- **`yfin_build_info` is set in the scheduler and the API** from
  `importlib.metadata.version`, which is the first thing that actually
  writes the gauge step 1 declared.

While implementing step 10 (image, compose, provisioning) and running the
stack against a live pipeline:

- **`job` is a RESERVED Prometheus label, and `yfin_job_*{job}` was
  broken.** A scrape stamps `job` and `instance` from the scrape config; a
  metric carrying its own `job` is not rejected but silently RENAMED to
  `exported_job`, and `job` becomes the scrape job's name. So every
  `by (job)` in a dashboard grouped by a label with one value and
  `JobOverdue` matched nothing. Found on the running stack, not in review:
  the exposition text looked right and only the ingested series was wrong.
  The label is now `job_name`, `job` and `instance` are OUT of
  `ALLOWED_LABELS`, and a test asserts no metric uses either.
- **`scheduler` and `stream` are in the BASE compose file, unprofiled,
  with `restart: unless-stopped`.** The design put `scheduler` in the
  `observability` profile and `stream` in a `stream` profile. Both are
  wrong for what they are: these two processes are what keeps the
  warehouse current, not part of the stack that watches it, and gating
  them behind a monitoring profile means the data stops being fresh
  whenever somebody brings the stack up without it. The two RELAYS keep a
  profile (`kafka`) -- both refuse to start when their feature is off, and
  a service that exits 1 under `restart: unless-stopped` is a crash loop
  rather than a disabled feature.
- **Non-API services disable the inherited HEALTHCHECK in the base file.**
  The image's check asks `127.0.0.1:8000/health`, which only the API
  serves; inherited, every other service reports permanently unhealthy,
  which is worse than no check. The observability override replaces it
  with a probe on the service's own `/metrics` port -- which also makes
  "the exporter thread is alive" a thing Docker checks.
- **Tempo 3.0 is not a 2.x config.** `grafana/tempo:3.0.3` replaced the
  ingester/compactor pair with a block-builder, a live-store and a backend
  scheduler/worker; the top-level `ingester:` and `compactor:` keys are a
  hard parse error (`field ingester not found in type app.Config`) and the
  container restarts forever. Retention is `backend_worker.compaction.
  block_retention`; `trace_idle_period` has no 3.x equivalent at this
  level and is dropped. Verified against the pinned image.
- **`alloy fmt --test`, not `--check`.** The flag in the design does not
  exist in v1.19.2. CI runs `fmt --test` through the pinned image, along
  with `promtool check config`, `promtool check rules`, and a
  `docker compose config` over both files.
- **Ports the stack publishes are parameterised.** `PROMETHEUS_PORT`,
  `GRAFANA_PORT` and `ALLOY_PORT` join the existing `DB_PORT`,
  `REDIS_PORT` and `API_PORT`, because a developer machine very often
  already has something on 9090 and 3000 -- this one did, and the stack
  would not start.
- **`tests/unit/test_observability_stack.py`** checks the two things a
  YAML linter cannot: that every metric named in a dashboard panel or an
  alert expression is DECLARED in `core/metrics.py`, and that the wiring
  between the compose files agrees with itself (one service per metrics
  port, `PROMETHEUS_MULTIPROC_DIR` on the API alone, every published port
  scraped).

While implementing step 11 (measurements and the integration run):

- **`stream reconcile` reported `failed` where the design says `locked`.**
  It takes the SYNC advisory lock and did not catch `LockNotAcquired`, so
  a collision reached the generic handler, exited 1, and the scheduler
  recorded `failed`. Found on the running stack: an hourly
  `stream_reconcile` behind a 33-hour backfill did this every hour, and
  would have fired `SyncFailed` and `JobPartial` for what is the advisory
  lock working exactly as designed. It now exits
  `EXIT_LOCK_NOT_ACQUIRED`; a test asserts every command that takes a lock
  maps it.
- **JSON logging is CHEAPER than console**, by about 20 % (15.3 µs against
  18.9 µs per line). The design assumed the opposite. `ConsoleRenderer`
  pads keys, aligns columns and decides colours; `JSONRenderer` is one
  `json.dumps` -- and the console figure was measured with colours OFF,
  which is the favourable case. The format is therefore chosen for who
  reads it and not for what it costs, which is what `LOG_FORMAT`'s default
  already does.
- **Tracing at sampling 1.0 costs 0.004 % of a symbol's wall clock.** 47
  spans per symbol at 16.7 µs each is 0.78 ms, against a symbol that takes
  20 seconds. The design's worry that a span per dataset was too many is
  not borne out.
- **The metrics endpoint holds the GIL for 0.0062 % of wall clock** at a
  15-second scrape: 0.93 ms to render 323 lines. Against `websocket.md`'s
  22,291 ticks/s ceiling that is 1.4 ticks of delay per scrape, inside a
  batch carrying hundreds. The regular-session comparison is still open --
  this was measured with the equity markets closed.
- **A full pass over 5,888 symbols on ONE IP takes 33 hours** at the
  measured 176 symbols/hour, so a nightly cadence cannot complete and
  every firing after the first exits `locked`. Freshness at that universe
  size is a proxy-pool decision, not a scheduler one: the pipeline shards
  one process per proxy, so the pass time divides by the pool size.
