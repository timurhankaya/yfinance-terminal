"""The `scheduler_runs` row: opened before the job, closed after it.

Its own module because the lifecycle is the part worth reading on its own.
A row exists from BEFORE the subprocess starts, so a scheduler that is
killed mid-job still leaves evidence that the job was attempted -- which is
the difference between "it failed" and "nobody knows", and the second is
what cron gave us.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from yfin.core.logging_setup import get_logger

log = get_logger(__name__)

#: Exit codes the pipeline defines, mapped to a word.
#:
#: 0 ok, 1 no symbols, 2 partial, 3 all failed, 4 lock not acquired, 5 no
#: proxy. `locked` is kept apart from `failed` because it is the expected
#: outcome of an overlap, not a fault: the previous run is still going and
#: the advisory lock did its job. `partial` likewise -- a nightly sync where
#: three symbols failed is not a failed sync.
_RESULT_BY_EXIT_CODE = {0: "ok", 2: "partial", 4: "locked"}


def result_for(exit_code: int) -> str:
    """The word for an exit code. Anything unmapped is a failure.

    Unmapped rather than enumerated: a code nobody planned for is a failure
    by definition, and listing 1, 3 and 5 explicitly would leave a future
    code 6 silently reported as `ok`.
    """
    return _RESULT_BY_EXIT_CODE.get(exit_code, "failed")


def open_run(
    factory: sessionmaker[Session], job: str, scheduled_at: datetime, pid: int | None = None
) -> int:
    """Records that a job is starting. Returns the row id.

    `scheduled_at` is when the TRIGGER said it should run, not now. The
    difference is the lateness a job accumulates waiting behind the
    single-threaded `yahoo` executor, and it is the number the dashboard
    shows.
    """
    with factory() as session:
        row = session.execute(
            text(
                "INSERT INTO scheduler_runs (job, scheduled_at, started_at, pid) "
                "VALUES (:job, :scheduled_at, :started_at, :pid) RETURNING id"
            ),
            {
                "job": job,
                "scheduled_at": scheduled_at,
                "started_at": datetime.now(UTC),
                "pid": pid,
            },
        ).scalar_one()
        session.commit()
    return int(row)


def close_run(
    factory: sessionmaker[Session],
    run_id: int,
    *,
    result: str,
    exit_code: int | None = None,
    error: str | None = None,
) -> None:
    """Records how it ended."""
    with factory() as session:
        session.execute(
            text(
                "UPDATE scheduler_runs "
                "   SET finished_at = :finished_at, result = :result, "
                "       exit_code = :exit_code, error = :error "
                " WHERE id = :id"
            ),
            {
                "id": run_id,
                "finished_at": datetime.now(UTC),
                "result": result,
                "exit_code": exit_code,
                "error": error,
            },
        )
        session.commit()


def record_unstarted(
    factory: sessionmaker[Session], job: str, scheduled_at: datetime, result: str
) -> None:
    """A firing that never became a subprocess.

    `misfired` -- it waited past its grace, usually behind a long job on the
    single-threaded executor -- or `skipped`, meaning a previous instance of
    the SAME job was still running. Both are opened and closed at once,
    because there was never a process in between, and both are recorded
    rather than logged: a job that quietly did not run is exactly what this
    table exists to make visible.
    """
    run_id = open_run(factory, job, scheduled_at)
    close_run(factory, run_id, result=result)


def close_orphans(factory: sessionmaker[Session]) -> int:
    """Closes rows left open by a scheduler that died. Returns the count.

    Run at start-up. A row with no `finished_at` belongs to a process this
    scheduler cannot see any more, so claiming to know how it ended would be
    a guess -- `terminated` says only that nobody closed it.

    If the subprocess somehow outlived its scheduler and is still holding
    the advisory lock, the next firing comes back `locked`. That is visible,
    which is the point; the alternative is two syncs overlapping in silence.
    """
    with factory() as session:
        result = session.execute(
            text(
                "UPDATE scheduler_runs "
                "   SET finished_at = :now, result = 'terminated', "
                "       error = 'scheduler restarted' "
                " WHERE finished_at IS NULL"
            ),
            {"now": datetime.now(UTC)},
        )
        session.commit()
        closed = int(getattr(result, "rowcount", 0) or 0)
    if closed:
        log.warning("closed scheduler runs left open by a previous process", count=closed)
    return closed


def close_orphan_sync_runs(factory: sessionmaker[Session]) -> int:
    """Closes sync audit rows whose scheduler parent was terminated.

    The scheduler and sync are separate processes.  On a scheduler restart,
    the scheduler row is recoverable, but the child may have been killed
    before its own finalizer ran.  Leaving that child as ``running`` makes
    freshness dashboards claim work is still in progress forever.  A run
    older than one day cannot be a healthy scheduled sync, so old manual
    rows without a scheduler parent are recovered too.
    """
    with factory() as session:
        result = session.execute(
            text(
                "UPDATE sync_runs AS sync "
                "   SET finished_at = :now, status = 'failed' "
                " WHERE sync.status = 'running' "
                "   AND sync.started_at < :cutoff "
                "   AND (sync.job_run_id IS NULL OR sync.job_run_id IN ("
                "       SELECT id FROM scheduler_runs WHERE result = 'terminated'"
                "   ))"
            ),
            {
                "now": datetime.now(UTC),
                "cutoff": datetime.now(UTC) - timedelta(days=1),
            },
        )
        session.commit()
        closed = int(getattr(result, "rowcount", 0) or 0)
    if closed:
        log.warning("closed sync runs left open by a terminated scheduler", count=closed)
    return closed


def last_success(factory: sessionmaker[Session]) -> dict[str, datetime]:
    """The newest successful finish per job.

    Read once at start-up to seed `yfin_job_last_success_timestamp`. Without
    it a restart would leave the gauge at zero and `JobOverdue` would fire
    on a system that is perfectly healthy.
    """
    with factory() as session:
        rows = session.execute(
            text(
                "SELECT job, max(finished_at) FROM scheduler_runs "
                " WHERE result = 'ok' GROUP BY job"
            )
        ).all()
    return {job: finished for job, finished in rows if finished is not None}


__all__ = [
    "close_orphans",
    "close_orphan_sync_runs",
    "close_run",
    "last_success",
    "open_run",
    "record_unstarted",
    "result_for",
]
