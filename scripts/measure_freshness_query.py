"""How long the freshness query takes on a full-size, synthesised audit table.

Everything is created in a throwaway schema and dropped at the end, so this
never touches the real audit tables. `--runs` is the axis that stresses the
cell/run index. Usage: measure_freshness_query.py [--runs 7] [--explain]"""

from __future__ import annotations

import argparse
import statistics
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from yfin.core.config import get_settings
from yfin.models.base import Base
from yfin.scheduler.queries import Context, freshness, freshness_sql

SCHEMA = "yfin_freshness_measurement"

#: The status mix of one synthetic night, in the proportions a healthy run
#: actually produces. A table of nothing but `ok` would hand the planner a
#: `worst` column with one value and a selectivity estimate no real install
#: ever sees.
STATUS_MIX = (("ok", 80), ("skipped", 15), ("empty", 3), ("failed", 2))

#: Thresholds a spread of 0..99 is bucketed by, from the mix above.
_BUCKETS = [
    (status, sum(share for _, share in STATUS_MIX[: index + 1]))
    for index, (status, _) in enumerate(STATUS_MIX)
]

#: Deterministic, and coprime with 100 so the two axes do not resonate into
#: a spread that is all one status for a whole symbol.
_SPREAD = "((s * 7 + d * 13) % 100)"

STATUS_CASE = (
    "CASE "
    + " ".join(f"WHEN {_SPREAD} < {edge} THEN '{status}'" for status, edge in _BUCKETS[:-1])
    + f" ELSE '{_BUCKETS[-1][0]}' END"
)

#: `symbols` is only here because `asof_state` has an FK to it: the
#: comparison below is the spec's open question -- whether `asof_state`
#: answers the same question more cheaply for the as-of datasets.
TABLES = ("symbols", "sync_runs", "sync_run_items", "asof_state")

#: What the exporter would pass: a daily `sync`, an hourly `market`, a
#: weekly `domain`, at the default freshness factor of 2.
THRESHOLDS = {
    "stale_symbols": 2 * 86_400.0,
    "stale_market": 2 * 3_600.0,
    "stale_domain": 2 * 604_800.0,
}


def _engine() -> Engine:
    """An engine whose every connection lives in the throwaway schema.

    Set on the connection rather than by qualifying the SQL: the query under
    measurement is the production one, unqualified."""
    settings = get_settings()
    return create_engine(
        settings.db_url(),
        connect_args={"options": f"-c search_path={SCHEMA},public"},
    )


def _create_schema(admin: Engine) -> None:
    with admin.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE'))
        conn.execute(text(f'CREATE SCHEMA "{SCHEMA}"'))


def _create_tables(engine: Engine) -> None:
    """Only the two audit tables, with their own indexes and enum types.

    `checkfirst=False` is load-bearing: `search_path` ends in `public`, so
    `checkfirst` would find the production table, create nothing, and every
    unqualified INSERT below would land in it."""
    tables = [Base.metadata.tables[name] for name in TABLES]
    with engine.begin() as conn:
        Base.metadata.create_all(conn, tables=tables, checkfirst=False)


def _assert_isolated(engine: Engine) -> None:
    """Refuses to go on unless the unqualified names land in the schema.

    Every statement here is unqualified on purpose, so where those names
    RESOLVE is the whole safety story."""
    # The SCHEMA the name resolves to, not `to_regclass`'s text: that
    # renders unqualified precisely when the table IS first on the
    # search_path, so comparing it against "schema.table" would reject the
    # only case that is correct.
    lookup = text(
        "SELECT n.nspname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.oid = to_regclass(:name)"
    )
    with engine.connect() as conn:
        for table in TABLES:
            resolved = conn.execute(lookup, {"name": table}).scalar_one_or_none()
            if resolved != SCHEMA:
                raise SystemExit(
                    f"refusing to run: `{table}` resolves to schema {resolved!r}, "
                    f"not {SCHEMA!r}. This script writes 1.5 million synthetic rows "
                    "and must never do so in a real schema."
                )


def _seed(session: Session, *, symbols: int, datasets: int, runs: int, now: datetime) -> int:
    """One run per night, every cell touched in each. Returns the row count.

    `generate_series` rather than a Python loop: the ORM would dominate the
    wall clock, and the read is what is being measured."""
    total = 0
    for index in range(runs):
        started = now - timedelta(days=runs - index)
        run_id = session.execute(
            text(
                "INSERT INTO sync_runs (started_at, scope, status, finished_at) "
                "VALUES (:started, 'symbols', 'ok', :finished) RETURNING id"
            ),
            {"started": started, "finished": started + timedelta(hours=2)},
        ).scalar_one()
        session.execute(
            text(
                "INSERT INTO sync_run_items "
                "  (run_id, symbol, dataset, status, shard_index) "
                "SELECT :run_id, 'SYM' || s::text, 'dataset_' || d::text, "
                f"       ({STATUS_CASE})::item_status, s % 8 "
                "FROM generate_series(1, :symbols) s, generate_series(1, :datasets) d"
            ),
            {"run_id": run_id, "symbols": symbols, "datasets": datasets},
        )
        total += symbols * datasets
    session.commit()
    return total


ASOF_SQL = """
SELECT dataset,
       COUNT(*) AS total,
       COUNT(*) FILTER (
           WHERE EXTRACT(EPOCH FROM (CAST(:now AS timestamptz) - fetched_at))
                 > CAST(:stale AS double precision)
       ) AS stale
FROM asof_state
GROUP BY 1
ORDER BY 1
"""


def _seed_asof(session: Session, *, symbols: int, datasets: int, now: datetime) -> int:
    """One row per (symbol, dataset), which is what `asof_state` already is.

    No run history: the table keeps the LATEST verification per cell, which
    is why it might be cheaper and why it cannot answer the same question."""
    session.execute(
        text(
            "INSERT INTO symbols (symbol, is_active) "
            "SELECT 'SYM' || s::text, true FROM generate_series(1, :symbols) s"
        ),
        {"symbols": symbols},
    )
    session.execute(
        text(
            "INSERT INTO asof_state "
            "  (symbol, dataset, as_of_date, content_hash, row_count, "
            "   first_seen_at, fetched_at) "
            "SELECT 'SYM' || s::text, 'dataset_' || d::text, "
            "       CAST(:now AS timestamptz)::date, repeat('a', 64), 1, "
            "       CAST(:now AS timestamptz), "
            "       CAST(:now AS timestamptz) - make_interval(hours => (s + d) % 96) "
            "FROM generate_series(1, :symbols) s, generate_series(1, :datasets) d"
        ),
        {"symbols": symbols, "datasets": datasets, "now": now},
    )
    session.commit()
    return symbols * datasets


def _time(factory: sessionmaker[Session], sql: str, params: dict[str, object],
          repeat: int) -> list[float]:
    durations: list[float] = []
    for _ in range(repeat):
        with factory() as session:
            started = time.perf_counter()
            session.execute(text(sql), params).all()
            durations.append(time.perf_counter() - started)
    return durations


def _report(label: str, durations: list[float]) -> None:
    print(
        f"{label}: min {min(durations):.3f}s  "
        f"median {statistics.median(durations):.3f}s  "
        f"max {max(durations):.3f}s  (n={len(durations)})"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", type=int, default=10_000)
    parser.add_argument("--datasets", type=int, default=49)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--repeat", type=int, default=5, help="Timed executions.")
    parser.add_argument("--explain", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    now = datetime.now(UTC)
    admin = create_engine(settings.db_url())
    _create_schema(admin)
    engine = _engine()

    print(
        f"schema {SCHEMA}: {args.symbols:,} symbols x {args.datasets} datasets "
        f"x {args.runs} runs"
    )
    try:
        _create_tables(engine)
        _assert_isolated(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        with factory() as session:
            started = time.perf_counter()
            rows = _seed(
                session,
                symbols=args.symbols,
                datasets=args.datasets,
                runs=args.runs,
                now=now,
            )
            print(f"seeded {rows:,} items in {time.perf_counter() - started:.1f}s")
            session.execute(text("ANALYZE sync_run_items"))
            session.execute(text("ANALYZE sync_runs"))
            session.commit()

        ctx = Context(
            session_factory=factory,
            settings=settings,
            intervals={"sync": 86_400.0, "market": 3_600.0, "domain": 604_800.0},
            now=now,
        )
        durations: list[float] = []
        samples = []
        for _ in range(args.repeat):
            started = time.perf_counter()
            samples = freshness(ctx)
            durations.append(time.perf_counter() - started)

        print(f"cells reported: {len(samples) // 2:,} series pairs")
        print(
            "freshness query: "
            f"min {min(durations):.3f}s  median {statistics.median(durations):.3f}s  "
            f"max {max(durations):.3f}s  (n={args.repeat})"
        )
        print(
            f"acceptance 5.000s at a {settings.yf_exporter_interval_seconds}s interval: "
            + ("PASS" if max(durations) < 5 else "FAIL")
        )

        # The spec's open question, and the operational one the plan
        # raises: both sorts spill to disk at the default `work_mem`.
        with factory() as session:
            rows = _seed_asof(
                session, symbols=args.symbols, datasets=args.datasets, now=now
            )
            session.execute(text("ANALYZE asof_state"))
            session.commit()
        print(f"\nasof_state: {rows:,} rows, one per cell, no run history")
        _report(
            "asof_state equivalent",
            _time(
                factory,
                ASOF_SQL,
                {"now": now, "stale": THRESHOLDS["stale_symbols"]},
                args.repeat,
            ),
        )

        with factory() as session:
            session.execute(text("SET work_mem = '256MB'"))
            started = time.perf_counter()
            session.execute(
                text(freshness_sql()), {"now": now, **THRESHOLDS}
            ).all()
            print(f"freshness at work_mem=256MB: {time.perf_counter() - started:.3f}s")

        if args.explain:
            with factory() as session:
                plan = session.execute(
                    text("EXPLAIN (ANALYZE, BUFFERS) " + freshness_sql()),
                    {"now": now, **THRESHOLDS},
                ).all()
            print()
            print("\n".join(row[0] for row in plan))
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE'))
        admin.dispose()


if __name__ == "__main__":
    main()
