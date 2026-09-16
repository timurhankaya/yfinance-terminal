"""What the exporter asks the database, and what it makes of the answers.

Every query takes a `Context` and returns `Sample`s; none touches
Prometheus. A query returns rows or raises, never a partial answer.
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

#: `sync_run_items.status` ordered by how bad it is, so a multi-table
#: dataset's items reduce to one status per run with a `MAX`. The two
#: out-of-universe statuses sit at the bottom: a cell leaves the universe
#: only when every table did.
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

    `intervals` is the cron cadence per job name, known only to the
    scheduler. `now` is passed so a test can place a row at a known age.
    """

    session_factory: sessionmaker[Session]
    settings: Settings
    intervals: dict[str, float]
    now: datetime = field(default_factory=lambda: datetime.now(UTC))

    def stale_after(self, scope: str) -> float:
        """Seconds after which a cell of this scope is stale.

        0 means "no cadence" (job unscheduled or unmeasurable); callers
        read it as "cannot judge", never divide by it.
        """
        job = SCOPE_JOBS.get(scope)
        if job is None:
            return 0.0
        return self.intervals.get(job, 0.0) * self.settings.yf_freshness_factor


def _values_clause(rows: dict[str, int]) -> str:
    """A literal `VALUES` list from a module constant.

    Interpolation is safe only because `rows` is a hard-coded constant,
    never a request or setting value.
    """
    return ", ".join(f"('{name}', {days})" for name, days in sorted(rows.items()))


# --- freshness -------------------------------------------------------------

#: A cell is `(symbol, region, dataset)`. `per_run` reduces a cell's items
#: to the worst status within one run; `latest` decides universe
#: membership; `good` is the newest verified run, whose `started_at` is
#: the age. `region` is NULL outside domain runs, hence `IS NOT DISTINCT FROM`.
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

    Substituted, not parameterised: a `CASE` body and `IN` lists are not
    values. They are module constants, so the string is stable per call.
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

    Not freshness: once the newest bar is older than the interval's
    retention depth, the gap can never be fetched again.
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

#: The latest run per (scope, kind); `kind` is `job_run_id IS NOT NULL`.
#: Both kinds are kept: a manual backfill must not overwrite the answer to
#: "did last night's job work".
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

#: `error_kind` is NULL where nothing classified the failure. Reported as
#: `unknown` rather than dropped, so the error count matches the item count.
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

#: Splits that apply and have not been applied: the two gates of
#: `storage/rescale.pending_splits`, over every symbol at once. The
#: earliest bar comes from one grouped pass; a correlated subquery per
#: split row is far too slow over the hypertable.
RESCALES_PENDING_SQL = """
WITH first_bar AS (
    SELECT symbol, MIN(local_date) AS local_date FROM price_bars GROUP BY symbol
)
SELECT COUNT(*) AS n
FROM splits s
JOIN first_bar b ON b.symbol = s.symbol
LEFT JOIN bar_rescales a
       ON a.symbol = s.symbol AND a.split_date = s.split_date
WHERE a.symbol IS NULL
  AND s.split_date > b.local_date
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

#: The OPEN session's connections only: `stream_connection_health` keeps
#: rows of finished sessions, and one stale row would pin the age gauges.
#: The ages stay MAXIMA over the surviving rows: one silent connection is
#: the failure, and an average would hide it.
_OPEN_SESSION = """
    JOIN stream_sessions s ON s.id = h.session_id AND s.finished_at IS NULL
"""

STREAM_HEALTH_SQL = """
SELECT COALESCE(MAX(EXTRACT(EPOCH FROM (CAST(:now AS timestamptz) - h.heartbeat_at))), 0)
           AS heartbeat_age,
       COALESCE(MAX(EXTRACT(EPOCH FROM (CAST(:now AS timestamptz) - h.last_canary_at))), 0)
           AS canary_age,
       COALESCE(SUM(h.subscribed_count), 0) AS subscribed,
       COALESCE(SUM(h.reconnect_count), 0) AS reconnects
FROM stream_connection_health h
{open_session}
"""

STREAM_STATES_SQL = """
SELECT h.state, COUNT(*) AS n
FROM stream_connection_health h
{open_session}
GROUP BY 1 ORDER BY 1
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
        health = session.execute(
            text(STREAM_HEALTH_SQL.format(open_session=_OPEN_SESSION)),
            {"now": ctx.now},
        ).one()
        states = session.execute(
            text(STREAM_STATES_SQL.format(open_session=_OPEN_SESSION))
        ).all()
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
    """How far behind each relay is, through the same `relay_lag` that `status` uses.

    `held_back_seconds` is not published: a backlog behind an open writing
    transaction is not a relay problem.
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
#: still in Redis and are not exported: mixing a flushed day with a live
#: one gives a number with no meaning at either end.
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

#: What a shard accumulated, for the latest run per scope, summed over
#: that run's shards; each shard flushed its own rows.
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

    A row whose name or labels this build does not declare is skipped:
    `run_metrics` keeps rows from renamed or retired counters.
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

    `gauges` is what the exporter clears before republishing; owning them
    per query keeps a failed query from wiping another's gauges.
    """

    name: str
    gauges: tuple[str, ...]
    run: Callable[[Context], list[Sample]]


def _republished_gauges() -> tuple[str, ...]:
    """The gauges `sync_counters` owns, derived from `METRICS` rather than retyped."""
    from yfin.core.metrics import METRICS

    return tuple(
        exported_name(spec.name)
        for spec in METRICS.values()
        if spec.kind == "counter" and spec.name.startswith("yfin_sync_")
    )


QUERIES: tuple[Query, ...] = (
    Query("freshness", ("yfin_cells_total", "yfin_cells_stale"), freshness),
    Query("intraday_scope", ("yfin_intraday_scope_stale",), intraday_scope_stale),
    Query(
        "audit",
        (
            "yfin_audit_items",
            "yfin_audit_errors",
            "yfin_audit_rows",
            "yfin_audit_status",
            "yfin_audit_running",
            "yfin_audit_started_timestamp",
        ),
        audit,
    ),
    Query("proxies", ("yfin_proxies",), proxies),
    Query(
        "bars",
        (
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
        (
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
        ("yfin_outbox_unpublished_rows", "yfin_outbox_oldest_age_seconds"),
        outboxes,
    ),
    Query(
        "api_usage",
        (
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

    Listed rather than discovered, so a renamed statement is not silently dropped.
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
        ("stream_health", STREAM_HEALTH_SQL.format(open_session=_OPEN_SESSION)),
        ("stream_states", STREAM_STATES_SQL.format(open_session=_OPEN_SESSION)),
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