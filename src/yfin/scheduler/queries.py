"""What the exporter asks the database, and what it makes of the answers.

Every function here takes a `Context` and returns a list of `Sample`. None of
them touch Prometheus: a query that also published would be a query no test
could read, and the whole point of separating the two is that the arithmetic
-- which cell is stale, which gap is about to expire -- is checkable against
rows without a registry in the picture.

The SQL is written out rather than built with the ORM. These are reporting
queries over eight tables with window functions, `DISTINCT ON` and `FILTER`;
the Core expression for the freshness query alone would be longer than the
statement and harder to compare against an `EXPLAIN`.

One rule the whole module obeys: **a query returns rows or raises**. It never
publishes a partial answer. The exporter catches, keeps the previous gauges
and lets `yfin_exporter_last_success_timestamp` go stale, which is the
signal `ExporterStale` alerts on -- half a refresh silently mixed with the
last one would not be.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from yfin.core.config import Settings
from yfin.core.metrics import exported_name
from yfin.datasets.bars import BAR_LIMITS
from yfin.models.sync import RunScope

#: Datasets a symbol run writes are stale against `sync`'s cadence, market
#: datasets against `market`'s, domain datasets against `domain`'s. The
#: scheduler already says how often each of those runs; a per-family "how
#: fresh should this be" setting would be the same number written twice.
SCOPE_JOBS: dict[str, str] = {
    RunScope.SYMBOLS.value: "sync",
    RunScope.MARKET.value: "market",
    RunScope.DOMAIN.value: "domain",
}

#: `sync_run_items.status`, ordered by how bad it is, so a multi-table
#: dataset's items can be reduced to one status per run with a `MAX`.
#:
#: `failed` on top: one failed table makes the cell failed for that run,
#: whatever the other tables did. `not_attempted` next, because a shard that
#: was pulled left real work undone and must show as a gap rather than as
#: nothing. The three good statuses in the middle. `out_of_scope` and
#: `unknown_symbol` at the BOTTOM, which is what makes the universe rule
#: work: a cell whose items are all out of scope reduces to `out_of_scope`
#: and leaves the universe, but one where a single table was written stays
#: in it and is judged on that write.
STATUS_RANK: dict[str, int] = {
    "out_of_scope": 0,
    "unknown_symbol": 1,
    "ok": 2,
    "empty": 3,
    "skipped": 4,
    "not_attempted": 5,
    "failed": 6,
}

#: Ranks that count as a verified write. `skipped` is in it because a
#: content-hash skip IS a verification: the row was fetched and compared.
#: The `--start/--end` date-range skip shares the status and slightly
#: flatters a manually ranged run, which is accepted rather than papered
#: over with a second status nobody else reads.
GOOD_RANKS = (STATUS_RANK["ok"], STATUS_RANK["empty"], STATUS_RANK["skipped"])

#: Ranks that leave the universe. `not_attempted` is NOT here: a shard that
#: was pulled must show as a gap, not vanish from the denominator.
OUT_OF_UNIVERSE_RANKS = (STATUS_RANK["out_of_scope"], STATUS_RANK["unknown_symbol"])

#: Intervals with a Yahoo retention edge, and how many days deep it is.
#: `1wk`/`1mo` have none -- Yahoo serves them back to 1980 -- so nothing
#: about them can expire and they are absent here rather than carrying a
#: sentinel.
BOUNDED_INTERVALS: dict[str, int] = {
    interval: depth for interval, (_, depth) in BAR_LIMITS.items() if depth is not None
}


@dataclass(frozen=True)
class Sample:
    """One gauge value with its labels, ready to publish."""

    name: str
    value: float
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class Context:
    """Everything a query needs that is not in the database.

    `intervals` is the cron cadence per job name, which only the scheduler
    knows -- it is derived from the trigger, not from a setting. `now` is
    passed rather than taken from `now()` in SQL so a test can place a row
    at a known age instead of sleeping.
    """

    session_factory: sessionmaker[Session]
    settings: Settings
    intervals: dict[str, float]
    now: datetime = field(default_factory=lambda: datetime.now(UTC))

    def stale_after(self, scope: str) -> float:
        """Seconds after which a cell of this scope is stale.

        0 means "no cadence": the job that writes these cells is not
        scheduled, or its cron fires less often than the 400-day sample
        window can measure. The callers read 0 as "cannot judge" rather
        than dividing by it, so an unscheduled job produces no staleness
        rather than a universe that is entirely stale.
        """
        job = SCOPE_JOBS.get(scope)
        if job is None:
            return 0.0
        return self.intervals.get(job, 0.0) * self.settings.yf_freshness_factor


def _values_clause(rows: dict[str, int]) -> str:
    """A literal `VALUES` list from a module constant.

    The only two callers pass `BOUNDED_INTERVALS`, whose keys are the
    hard-coded interval names in `models/bars.py` and whose values are
    integers from `BAR_LIMITS`. Nothing here comes from a request or a
    setting, which is why it can be interpolated at all -- and why a
    caller passing anything else would be the bug to catch in review.
    """
    return ", ".join(f"('{name}', {days})" for name, days in sorted(rows.items()))


# --- freshness -------------------------------------------------------------

#: A cell is `(symbol, region, dataset)` -- exactly the identity
#: `sync_run_items` records, with `region` NULL outside domain runs and
#: `symbol` the scope label for market runs.
#:
#: Three steps, and the middle one is the one that is easy to get wrong. A
#: multi-table dataset writes one item PER TABLE per run, each with its own
#: status, so `per_run` reduces a cell's items to the worst status IN THAT
#: RUN first. `latest` then takes the newest run for the cell, and its
#: status decides whether the cell is in the universe at all. `good` takes
#: the newest run in which the cell was actually verified, and its
#: `started_at` is the age staleness is measured from -- which is a
#: DIFFERENT run whenever last night's attempt failed.
#:
#: The join back to `good` uses `IS NOT DISTINCT FROM` on `region`: it is
#: NULL for every symbol and market cell, and `=` would match none of them
#: and report the entire universe stale.
FRESHNESS_SQL = """
WITH per_run AS (
    SELECT run_id, symbol, region, dataset,
           MAX(CASE status {ranks} END) AS worst
    FROM sync_run_items
    GROUP BY run_id, symbol, region, dataset
),
cell AS (
    SELECT p.symbol, p.region, p.dataset,
           (array_agg(p.worst ORDER BY p.run_id DESC))[1] AS worst,
           (array_agg(r.scope::text ORDER BY p.run_id DESC))[1] AS scope,
           MAX(r.started_at) FILTER (WHERE p.worst IN {good_ranks}) AS last_good
    FROM per_run p
    JOIN sync_runs r ON r.id = p.run_id
    GROUP BY p.symbol, p.region, p.dataset
)
SELECT c.scope AS scope,
       c.dataset AS dataset,
       COUNT(*) AS total,
       COUNT(*) FILTER (
           WHERE {threshold} > 0
             AND (c.last_good IS NULL
                  OR EXTRACT(EPOCH FROM (CAST(:now AS timestamptz) - c.last_good))
                     > {threshold})
       ) AS stale
FROM cell c
WHERE c.worst NOT IN {out_ranks}
GROUP BY 1, 2
ORDER BY 1, 2
"""

#: The per-scope staleness threshold, as one SQL expression. Three
#: parameters rather than three queries: the universe is one scan and
#: splitting it by scope would read `sync_run_items` three times.
_THRESHOLD = (
    "CASE c.scope "
    "WHEN 'market' THEN CAST(:stale_market AS double precision) "
    "WHEN 'domain' THEN CAST(:stale_domain AS double precision) "
    "ELSE CAST(:stale_symbols AS double precision) END"
)


def freshness_sql() -> str:
    """The freshness statement, with its constants substituted in.

    Substituted rather than parameterised because they are not values: the
    status ranking is a `CASE` body and the two membership tests are `IN`
    lists whose length is part of the plan. All three come from module
    constants, so the statement is the same string on every call and
    PostgreSQL can reuse the plan.
    """
    ranks = " ".join(f"WHEN '{status}' THEN {rank}" for status, rank in STATUS_RANK.items())
    return FRESHNESS_SQL.format(
        ranks=ranks,
        good_ranks=f"({', '.join(str(r) for r in GOOD_RANKS)})",
        out_ranks=f"({', '.join(str(r) for r in OUT_OF_UNIVERSE_RANKS)})",
        threshold=_THRESHOLD,
    )


def freshness(ctx: Context) -> list[Sample]:
    """`yfin_cells_stale` and `yfin_cells_total`, per scope and dataset."""
    with ctx.session_factory() as session:
        rows = session.execute(
            text(freshness_sql()),
            {
                "now": ctx.now,
                "stale_symbols": ctx.stale_after(RunScope.SYMBOLS.value),
                "stale_market": ctx.stale_after(RunScope.MARKET.value),
                "stale_domain": ctx.stale_after(RunScope.DOMAIN.value),
            },
        ).all()
    samples: list[Sample] = []
    for scope, dataset, total, stale in rows:
        labels = {"scope": scope, "dataset": dataset}
        samples.append(Sample("yfin_cells_total", float(total), labels))
        samples.append(Sample("yfin_cells_stale", float(stale), labels))
    return samples


INTRADAY_SCOPE_SQL = """
SELECT s.bar_interval AS interval,
       COUNT(*) FILTER (
           WHERE n.newest IS NULL
              OR n.newest < CAST(:now AS timestamptz)
                            - make_interval(days => GREATEST(lim.depth - :warn, 0))
       ) AS stale
FROM intraday_scope s
JOIN (VALUES {limits}) AS lim(bar_interval, depth)
  ON lim.bar_interval = s.bar_interval
LEFT JOIN LATERAL (
    SELECT MAX(b.ts_utc) AS newest
    FROM price_bars b
    WHERE b.symbol = s.symbol AND b.bar_interval = s.bar_interval
) n ON TRUE
WHERE s.enabled
GROUP BY 1
ORDER BY 1
"""


def intraday_scope_stale(ctx: Context) -> list[Sample]:
    """Symbols whose newest bar is about to fall off Yahoo's window.

    Not the same question as freshness. A cell can be perfectly fresh by
    the schedule and still be unrecoverable: once the newest bar is older
    than the interval's retention depth, the window between it and now can
    never be fetched again, and the archive has a permanent hole. The warn
    margin is how much notice that leaves.
    """
    with ctx.session_factory() as session:
        rows = session.execute(
            text(INTRADAY_SCOPE_SQL.format(limits=_values_clause(BOUNDED_INTERVALS))),
            {"now": ctx.now, "warn": ctx.settings.yf_intraday_retention_warn_days},
        ).all()
    return [
        Sample("yfin_intraday_scope_stale", float(stale), {"interval": interval})
        for interval, stale in rows
    ]


# --- correctness -----------------------------------------------------------

#: The latest run per (scope, kind), where `kind` is whether the scheduler
#: started it. `job_run_id IS NOT NULL` is the whole test: `audit.open_run`
#: reads `YF_JOB_RUN_ID` from the environment, so a run is scheduled exactly
#: when a scheduler put it there.
#:
#: Both kinds are kept because they answer different questions. "Did last
#: night's job work" is about the scheduled one; a manual backfill must not
#: overwrite that answer, and must still be visible while it runs.
LATEST_RUNS_SQL = """
SELECT scope, kind, id, started_at, finished_at, status,
       rows_fetched, rows_written, rows_verified, rows_skipped
FROM (
    SELECT r.scope::text AS scope,
           CASE WHEN r.job_run_id IS NOT NULL THEN 'scheduled' ELSE 'manual' END AS kind,
           r.id, r.started_at, r.finished_at, r.status::text AS status,
           r.rows_fetched, r.rows_written, r.rows_verified, r.rows_skipped,
           ROW_NUMBER() OVER (
               PARTITION BY r.scope, (r.job_run_id IS NOT NULL)
               ORDER BY r.started_at DESC, r.id DESC
           ) AS rn
    FROM sync_runs r
) ranked
WHERE rn = 1
"""

ITEMS_SQL = """
SELECT run_id, status::text AS status, COUNT(*) AS n
FROM sync_run_items
WHERE run_id = ANY(:ids)
GROUP BY 1, 2
"""

#: `error_kind` is NULL where nothing classified the failure -- a crashed
#: worker before step 5's plumbing, or a path that never reached
#: `classify_error`. Reported as `unknown` rather than dropped: a failure
#: with no kind is still a failure, and a gauge that silently omitted it
#: would make the error count disagree with the item count.
ERRORS_SQL = """
SELECT run_id, COALESCE(error_kind, 'unknown') AS error_kind, COUNT(*) AS n
FROM sync_run_items
WHERE run_id = ANY(:ids) AND status = 'failed'
GROUP BY 1, 2
"""

#: `yfin_audit_status`. Numbers, not a label: a status LABEL would make
#: every alert a string comparison against a series that only exists while
#: that status holds, and "is the last run worse than ok" is the question.
RUN_STATUS_VALUE: dict[str, float] = {"ok": 0, "partial": 1, "failed": 2}

ROW_MEASURES = ("fetched", "written", "verified", "skipped")


def audit(ctx: Context) -> list[Sample]:
    """What the latest run per scope and kind did, and how it ended."""
    with ctx.session_factory() as session:
        runs = session.execute(text(LATEST_RUNS_SQL)).mappings().all()
        ids = [row["id"] for row in runs]
        items = session.execute(text(ITEMS_SQL), {"ids": ids}).all() if ids else []
        errors = session.execute(text(ERRORS_SQL), {"ids": ids}).all() if ids else []

    where = {row["id"]: (row["scope"], row["kind"]) for row in runs}
    samples: list[Sample] = []
    running: dict[str, float] = {}

    for row in runs:
        scope, kind = row["scope"], row["kind"]
        labels = {"scope": scope, "kind": kind}
        samples.append(
            Sample("yfin_audit_started_timestamp", row["started_at"].timestamp(), labels)
        )
        for measure in ROW_MEASURES:
            samples.append(
                Sample(
                    "yfin_audit_rows",
                    float(row[f"rows_{measure}"]),
                    {**labels, "measure": measure},
                )
            )
        # Only a FINISHED run has a verdict. A running one has `status =
        # 'running'`, which is not a grade, and publishing it as one would
        # make every long sync look like a new failure mode.
        if row["finished_at"] is not None and row["status"] in RUN_STATUS_VALUE:
            samples.append(
                Sample("yfin_audit_status", RUN_STATUS_VALUE[row["status"]], labels)
            )
        running[scope] = max(running.get(scope, 0.0), float(row["status"] == "running"))

    for run_id, status, count in items:
        scope, kind = where[run_id]
        samples.append(
            Sample(
                "yfin_audit_items",
                float(count),
                {"scope": scope, "kind": kind, "status": status},
            )
        )
    for run_id, error_kind, count in errors:
        scope, kind = where[run_id]
        samples.append(
            Sample(
                "yfin_audit_errors",
                float(count),
                {"scope": scope, "kind": kind, "error_kind": error_kind},
            )
        )
    samples.extend(
        Sample("yfin_audit_running", value, {"scope": scope})
        for scope, value in sorted(running.items())
    )
    return samples


#: `is_enabled` is the operator's decision and `health` is the system's
#: observation, so a disabled proxy reports as `disabled` whatever health it
#: last had -- the pool's usable size is the sum of the healthy and unknown
#: rows, and a disabled-but-healthy proxy in that sum would overstate it.
PROXIES_SQL = """
SELECT CASE WHEN is_enabled THEN health::text ELSE 'disabled' END AS state,
       COUNT(*) AS n
FROM proxies
GROUP BY 1
ORDER BY 1
"""


def proxies(ctx: Context) -> list[Sample]:
    with ctx.session_factory() as session:
        rows = session.execute(text(PROXIES_SQL)).all()
    return [Sample("yfin_proxies", float(n), {"state": state}) for state, n in rows]


# --- bars ------------------------------------------------------------------

GAPS_OPEN_SQL = """
SELECT bar_interval, reason, COUNT(*) AS n
FROM bar_gaps
WHERE resolved_at IS NULL
GROUP BY 1, 2
ORDER BY 1, 2
"""

GAPS_OLDEST_SQL = """
SELECT bar_interval,
       EXTRACT(EPOCH FROM (CAST(:now AS timestamptz) - MIN(gap_start_utc))) AS age
FROM bar_gaps
WHERE resolved_at IS NULL
GROUP BY 1
ORDER BY 1
"""

#: An open gap whose window is about to leave Yahoo's reach. After that
#: edge the only thing that can still close it is `stream reconcile` from
#: the tick archive, and only for 1m -- which is exactly why the warning
#: has to arrive before the edge rather than at it.
GAPS_EXPIRING_SQL = """
SELECT g.bar_interval, COUNT(*) AS n
FROM bar_gaps g
JOIN (VALUES {limits}) AS lim(bar_interval, depth)
  ON lim.bar_interval = g.bar_interval
WHERE g.resolved_at IS NULL
  AND g.gap_start_utc < CAST(:now AS timestamptz)
                        - make_interval(days => GREATEST(lim.depth - :warn, 0))
GROUP BY 1
ORDER BY 1
"""

GAPS_RESOLVED_SQL = """
SELECT bar_interval, COALESCE(resolved_by, 'unknown') AS resolved_by, COUNT(*) AS n
FROM bar_gaps
WHERE resolved_at IS NOT NULL
GROUP BY 1, 2
ORDER BY 1, 2
"""

#: Splits that apply and have not been applied. Both gates from
#: `storage/rescale.pending_splits`, over every symbol at once: no
#: `bar_rescales` row, and a split date after the symbol's earliest bar. The
#: second gate is what keeps a fresh install -- where `rescale --seed` was
#: skipped -- from reporting every historical split as pending work.
RESCALES_PENDING_SQL = """
SELECT COUNT(*) AS n
FROM splits s
LEFT JOIN bar_rescales a
       ON a.symbol = s.symbol AND a.split_date = s.split_date
WHERE a.symbol IS NULL
  AND s.split_date > (
      SELECT MIN(b.local_date) FROM price_bars b WHERE b.symbol = s.symbol
  )
"""


def bars(ctx: Context) -> list[Sample]:
    """Open gaps, their age, the ones about to expire, and pending rescales."""
    params = {"now": ctx.now, "warn": ctx.settings.yf_intraday_retention_warn_days}
    with ctx.session_factory() as session:
        open_rows = session.execute(text(GAPS_OPEN_SQL)).all()
        oldest = session.execute(text(GAPS_OLDEST_SQL), {"now": ctx.now}).all()
        expiring = session.execute(
            text(GAPS_EXPIRING_SQL.format(limits=_values_clause(BOUNDED_INTERVALS))),
            params,
        ).all()
        resolved = session.execute(text(GAPS_RESOLVED_SQL)).all()
        pending = session.execute(text(RESCALES_PENDING_SQL)).scalar_one()

    samples = [
        Sample("yfin_bar_gaps_open", float(n), {"interval": interval, "reason": reason})
        for interval, reason, n in open_rows
    ]
    samples += [
        Sample("yfin_bar_gaps_oldest_age_seconds", float(age), {"interval": interval})
        for interval, age in oldest
    ]
    samples += [
        Sample("yfin_bar_gaps_expiring", float(n), {"interval": interval})
        for interval, n in expiring
    ]
    samples += [
        Sample(
            "yfin_bar_gaps_resolved",
            float(n),
            {"interval": interval, "resolved_by": resolved_by},
        )
        for interval, resolved_by, n in resolved
    ]
    samples.append(Sample("yfin_bar_rescales_pending", float(pending)))
    return samples


# --- stream and the outboxes -----------------------------------------------

#: `stream_connection_health` holds CURRENT state only, one row per
#: connection. The two ages are maxima across the rows because one silent
#: connection is the failure: averaging them would hide it behind the ones
#: still working.
STREAM_HEALTH_SQL = """
SELECT COALESCE(MAX(EXTRACT(EPOCH FROM (CAST(:now AS timestamptz) - heartbeat_at))), 0)
           AS heartbeat_age,
       COALESCE(MAX(EXTRACT(EPOCH FROM (CAST(:now AS timestamptz) - last_canary_at))), 0)
           AS canary_age,
       COALESCE(SUM(subscribed_count), 0) AS subscribed,
       COALESCE(SUM(reconnect_count), 0) AS reconnects
FROM stream_connection_health
"""

STREAM_STATES_SQL = """
SELECT state, COUNT(*) AS n FROM stream_connection_health GROUP BY 1 ORDER BY 1
"""

#: The OPEN session -- there is at most one, because `stream run` takes an
#: advisory lock. A finished session's totals are history and belong in a
#: query, not on a gauge that would freeze at the last value forever.
STREAM_SESSION_SQL = """
SELECT messages_received, rows_rejected
FROM stream_sessions
WHERE finished_at IS NULL
ORDER BY started_at DESC
LIMIT 1
"""


def stream(ctx: Context) -> list[Sample]:
    with ctx.session_factory() as session:
        health = session.execute(text(STREAM_HEALTH_SQL), {"now": ctx.now}).one()
        states = session.execute(text(STREAM_STATES_SQL)).all()
        current = session.execute(text(STREAM_SESSION_SQL)).one_or_none()

    heartbeat_age, canary_age, subscribed, reconnects = health
    samples = [
        Sample("yfin_stream_heartbeat_age_seconds", float(heartbeat_age)),
        Sample("yfin_stream_canary_age_seconds", float(canary_age)),
        Sample("yfin_stream_subscribed_symbols", float(subscribed)),
        Sample("yfin_stream_reconnects", float(reconnects)),
    ]
    samples += [
        Sample("yfin_stream_connections", float(n), {"state": state}) for state, n in states
    ]
    if current is not None:
        samples.append(Sample("yfin_stream_messages", float(current[0])))
        samples.append(Sample("yfin_stream_rejects", float(current[1])))
    return samples


def outboxes(ctx: Context) -> list[Sample]:
    """How far behind each relay is, through the same call `... status` uses.

    `relay_lag` rather than a query of its own: the two cursors count lag
    differently -- `id` and `(xid, id)` -- and a second implementation here
    would be the one that gets it wrong on the outbox with concurrent
    writers.

    `held_back_seconds` is deliberately not published. A backlog behind an
    open writing transaction is not a relay problem, and the alert reads
    `unpublished_rows` and the age; the third number is what the operator
    then runs `yfin changes status` to see.
    """
    from yfin.outbox.relay import relay_lag
    from yfin.outbox.spec import CHANGES_OUTBOX, TICK_OUTBOX

    samples: list[Sample] = []
    for spec in (TICK_OUTBOX, CHANGES_OUTBOX):
        lag = relay_lag(ctx.session_factory, spec)
        labels = {"outbox": spec.table}
        samples.append(Sample("yfin_outbox_unpublished_rows", float(lag.rows), labels))
        samples.append(
            Sample("yfin_outbox_oldest_age_seconds", float(lag.oldest_age_seconds), labels)
        )
    return samples


# --- API usage -------------------------------------------------------------

#: `api_usage_daily` holds days that have been FLUSHED. Today's counts are
#: still in Redis and are deliberately not exported: the API would have to
#: be reached to read them, and a gauge that is half a flushed day and half
#: a live one is a number with no meaning at either end.
API_USAGE_SQL = """
WITH latest AS (SELECT MAX(day) AS day FROM api_usage_daily)
SELECT u.endpoint_family::text AS family,
       SUM(u.request_count) AS requests,
       EXTRACT(EPOCH FROM (SELECT day FROM latest)::timestamptz) AS day_ts
FROM api_usage_daily u, latest
WHERE u.day = latest.day
GROUP BY 1, 3
ORDER BY 1
"""

API_ESTIMATED_SQL = "SELECT COUNT(DISTINCT day) FROM api_usage_daily WHERE estimated"


def api_usage(ctx: Context) -> list[Sample]:
    with ctx.session_factory() as session:
        rows = session.execute(text(API_USAGE_SQL)).all()
        estimated = session.execute(text(API_ESTIMATED_SQL)).scalar_one()

    samples = [Sample("yfin_api_usage_estimated_days", float(estimated))]
    for family, requests, day_ts in rows:
        samples.append(Sample("yfin_api_usage_requests", float(requests), {"family": family}))
        samples.append(Sample("yfin_api_usage_day_timestamp", float(day_ts)))
    return samples


# --- the sync counters, read back out of run_metrics -----------------------

#: What a shard accumulated, for the latest run per scope, summed over that
#: run's shards.
#:
#: The sum over `shard_index` is the point: shards are separate processes
#: started with `spawn`, so each flushed its own rows and the run's real
#: count exists nowhere until they are added up here.
RUN_METRICS_SQL = """
WITH latest AS (
    SELECT DISTINCT ON (scope) id, scope::text AS scope
    FROM sync_runs
    ORDER BY scope, started_at DESC, id DESC
)
SELECT l.scope, m.name, m.labels, SUM(m.value) AS value
FROM run_metrics m
JOIN latest l ON l.id = m.run_id
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3
"""


def sync_counters(ctx: Context) -> list[Sample]:
    """`yfin_sync_*{scope,...}` from `run_metrics`.

    A row whose name or labels this build does not declare is SKIPPED, not
    published. `run_metrics` keeps history: a counter that was renamed or
    retired still has rows from the runs that wrote it, and publishing them
    would register a series with a label set the declaration no longer
    matches -- which `prometheus_client` refuses anyway, one metric at a
    time, mid-refresh.
    """
    import json

    from yfin.core.metrics import METRICS

    with ctx.session_factory() as session:
        rows = session.execute(text(RUN_METRICS_SQL)).all()

    samples: list[Sample] = []
    for scope, name, labels, value in rows:
        spec = METRICS.get(exported_name(name))
        if spec is None:
            continue
        parsed = json.loads(labels)
        if set(parsed) | {"scope"} != set(spec.labelnames):
            continue
        samples.append(Sample(spec.name, float(value), {"scope": scope, **parsed}))
    return samples


# --- the registry ----------------------------------------------------------


@dataclass(frozen=True)
class Query:
    """One refresh step: what it is called, what it owns, and how to run it.

    `gauges` is what the exporter CLEARS before republishing this query's
    samples, and clearing is the only way a label combination ever goes
    away -- a dataset that leaves the universe, a proxy that is deleted, a
    stream connection that closes. Owning them by query rather than
    globally is what keeps a failed query from wiping gauges another one
    filled in the same pass.
    """

    name: str
    gauges: tuple[str, ...]
    run: Callable[[Context], list[Sample]]


def _owned(*names: str) -> tuple[str, ...]:
    return names


def _republished_gauges() -> tuple[str, ...]:
    """The gauges `sync_counters` owns, derived rather than retyped.

    A counter added to `core/metrics.METRICS` gets its exported gauge
    cleared and refreshed with no edit here, and a list written out by hand
    would go stale exactly when a new counter is the thing being watched.
    """
    from yfin.core.metrics import METRICS

    return tuple(
        exported_name(spec.name)
        for spec in METRICS.values()
        if spec.kind == "counter" and spec.name.startswith("yfin_sync_")
    )


QUERIES: tuple[Query, ...] = (
    Query("freshness", _owned("yfin_cells_total", "yfin_cells_stale"), freshness),
    Query("intraday_scope", _owned("yfin_intraday_scope_stale"), intraday_scope_stale),
    Query(
        "audit",
        _owned(
            "yfin_audit_items",
            "yfin_audit_errors",
            "yfin_audit_rows",
            "yfin_audit_status",
            "yfin_audit_running",
            "yfin_audit_started_timestamp",
        ),
        audit,
    ),
    Query("proxies", _owned("yfin_proxies"), proxies),
    Query(
        "bars",
        _owned(
            "yfin_bar_gaps_open",
            "yfin_bar_gaps_oldest_age_seconds",
            "yfin_bar_gaps_expiring",
            "yfin_bar_gaps_resolved",
            "yfin_bar_rescales_pending",
        ),
        bars,
    ),
    Query(
        "stream",
        _owned(
            "yfin_stream_connections",
            "yfin_stream_heartbeat_age_seconds",
            "yfin_stream_canary_age_seconds",
            "yfin_stream_subscribed_symbols",
            "yfin_stream_reconnects",
            "yfin_stream_messages",
            "yfin_stream_rejects",
        ),
        stream,
    ),
    Query(
        "outboxes",
        _owned("yfin_outbox_unpublished_rows", "yfin_outbox_oldest_age_seconds"),
        outboxes,
    ),
    Query(
        "api_usage",
        _owned(
            "yfin_api_usage_requests",
            "yfin_api_usage_day_timestamp",
            "yfin_api_usage_estimated_days",
        ),
        api_usage,
    ),
    Query("sync_counters", _republished_gauges(), sync_counters),
)


def statements() -> Iterable[tuple[str, str]]:
    """Every SQL statement in this module, named. What the compile test reads.

    Listed rather than discovered: a `dir()` sweep would also pick up any
    string constant that happened to end in `_SQL`, and would silently stop
    covering a statement someone renamed.
    """
    limits = _values_clause(BOUNDED_INTERVALS)
    return (
        ("freshness", freshness_sql()),
        ("intraday_scope", INTRADAY_SCOPE_SQL.format(limits=limits)),
        ("latest_runs", LATEST_RUNS_SQL),
        ("items", ITEMS_SQL),
        ("errors", ERRORS_SQL),
        ("proxies", PROXIES_SQL),
        ("gaps_open", GAPS_OPEN_SQL),
        ("gaps_oldest", GAPS_OLDEST_SQL),
        ("gaps_expiring", GAPS_EXPIRING_SQL.format(limits=limits)),
        ("gaps_resolved", GAPS_RESOLVED_SQL),
        ("rescales_pending", RESCALES_PENDING_SQL),
        ("stream_health", STREAM_HEALTH_SQL),
        ("stream_states", STREAM_STATES_SQL),
        ("stream_session", STREAM_SESSION_SQL),
        ("api_usage", API_USAGE_SQL),
        ("api_estimated", API_ESTIMATED_SQL),
        ("run_metrics", RUN_METRICS_SQL),
    )


__all__: list[str] = [
    "BOUNDED_INTERVALS",
    "GOOD_RANKS",
    "OUT_OF_UNIVERSE_RANKS",
    "QUERIES",
    "SCOPE_JOBS",
    "STATUS_RANK",
    "Context",
    "Query",
    "Sample",
    "api_usage",
    "audit",
    "bars",
    "freshness",
    "freshness_sql",
    "intraday_scope_stale",
    "outboxes",
    "proxies",
    "statements",
    "stream",
    "sync_counters",
]