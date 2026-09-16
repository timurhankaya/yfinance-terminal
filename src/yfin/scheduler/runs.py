"""The `scheduler_runs` row: opened before the job, closed after it.

The row exists before the subprocess starts, so a scheduler killed
mid-job still leaves evidence the job was attempted.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from yfin.core.logging_setup import get_logger

log = get_logger(__name__)

#: Pipeline exit codes mapped to a word; anything unmapped is `failed`.
#: `locked` and `partial` are not faults: an overlap or a few failed
#: symbols is not a failed sync.
_RESULT_BY_EXIT_CODE = {0: "ok", 2: "partial", 4: "locked"}


def result_for(exit_code: int) -> str:
    """The word for an exit code. Anything unmapped is a failure."""
    return _RESULT_BY_EXIT_CODE.get(exit_code, "failed")


def open_run(
    factory: sessionmaker[Session], job: str, scheduled_at: datetime, pid: int | None = None
) -> int:
    """Records that a job is starting. Returns the row id.

    `scheduled_at` is the trigger's time, not now; the difference is the
    lateness the dashboard shows.
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
    """A firing that never became a subprocess: `misfired` or `skipped`.

    Recorded rather than logged: a job that quietly did not run is what
    this table exists to make visible.
    """
    run_id = open_run(factory, job, scheduled_at)
    close_run(factory, run_id, result=result)


def close_orphans(factory: sessionmaker[Session]) -> int:
    """Closes rows left open by a scheduler that died. Returns the count.

    Run at start-up. `terminated` says only that nobody closed the row;
    a subprocess that outlived its scheduler surfaces as `locked` next time.
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

    The child may have been killed before its own finalizer ran. A run
    older than one day cannot be healthy, so old manual rows are closed too.
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

    Seeds `yfin_job_last_success_timestamp` at start-up so a restart does
    not fire `JobOverdue`.
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
