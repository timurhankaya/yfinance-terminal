"""The `scheduler_runs` lifecycle against a real database.

The row exists from BEFORE the subprocess starts, which is the difference
between "it failed" and "nobody knows" -- and the second is what cron gave
us.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from yfin.models.ops import SchedulerRun
from yfin.models.sync import RunScope, RunStatus, SyncRun
from yfin.scheduler import runs

pytestmark = pytest.mark.repo

SCHEDULED = datetime(2026, 9, 7, 2, 0, tzinfo=UTC)


@pytest.fixture
def factory(db_session: Session) -> sessionmaker[Session]:
    return sessionmaker(
        bind=db_session.connection(),
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )


def _rows(session: Session) -> list[SchedulerRun]:
    return list(
        session.execute(select(SchedulerRun).order_by(SchedulerRun.id)).scalars()
    )


class TestOpenAndClose:
    def test_a_row_exists_before_the_job_ends(
        self, factory: sessionmaker[Session], db_session: Session
    ) -> None:
        """A scheduler killed mid-job still leaves evidence the job was
        attempted."""
        runs.open_run(factory, "sync", SCHEDULED, pid=4242)
        (row,) = _rows(db_session)
        assert row.job == "sync"
        assert row.started_at is not None
        assert row.finished_at is None
        assert row.pid == 4242

    def test_scheduled_at_is_the_trigger_time_not_now(
        self, factory: sessionmaker[Session], db_session: Session
    ) -> None:
        """Their difference is the lateness a job accumulates waiting behind
        the single-threaded executor, and it is what the dashboard shows."""
        runs.open_run(factory, "sync", SCHEDULED)
        (row,) = _rows(db_session)
        assert row.scheduled_at == SCHEDULED
        assert row.started_at != SCHEDULED

    def test_closing_records_the_outcome(
        self, factory: sessionmaker[Session], db_session: Session
    ) -> None:
        run_id = runs.open_run(factory, "sync", SCHEDULED)
        runs.close_run(factory, run_id, result="partial", exit_code=2)
        (row,) = _rows(db_session)
        assert (row.result, row.exit_code) == ("partial", 2)
        assert row.finished_at is not None

    def test_both_the_code_and_the_word_are_kept(
        self, factory: sessionmaker[Session], db_session: Session
    ) -> None:
        """The mapping can be revisited later; a stored code lets that happen
        without the history becoming a lie."""
        run_id = runs.open_run(factory, "sync", SCHEDULED)
        runs.close_run(factory, run_id, result="failed", exit_code=127)
        (row,) = _rows(db_session)
        assert (row.exit_code, row.result) == (127, "failed")


class TestUnstarted:
    """A firing that never became a subprocess is still recorded.

    A job that quietly did not run is exactly what this table exists to make
    visible.
    """

    @pytest.mark.parametrize("result", ["misfired", "skipped"])
    def test_it_is_opened_and_closed_at_once(
        self, factory: sessionmaker[Session], db_session: Session, result: str
    ) -> None:
        runs.record_unstarted(factory, "stream_reconcile", SCHEDULED, result)
        (row,) = _rows(db_session)
        assert row.result == result
        assert row.finished_at is not None
        assert row.exit_code is None


class TestOrphans:
    def test_a_row_left_open_is_closed_as_terminated(
        self, factory: sessionmaker[Session], db_session: Session
    ) -> None:
        """Claiming to know how it ended would be a guess; `terminated` says
        only that nobody closed it."""
        runs.open_run(factory, "sync", SCHEDULED)
        assert runs.close_orphans(factory) == 1
        (row,) = _rows(db_session)
        assert row.result == "terminated"
        assert row.error == "scheduler restarted"
        assert row.finished_at is not None

    def test_a_closed_row_is_left_alone(
        self, factory: sessionmaker[Session], db_session: Session
    ) -> None:
        run_id = runs.open_run(factory, "sync", SCHEDULED)
        runs.close_run(factory, run_id, result="ok", exit_code=0)
        assert runs.close_orphans(factory) == 0
        (row,) = _rows(db_session)
        assert row.result == "ok"

    def test_nothing_open_is_not_an_error(
        self, factory: sessionmaker[Session], db_session: Session
    ) -> None:
        assert runs.close_orphans(factory) == 0
        assert db_session.execute(
            select(func.count()).select_from(SchedulerRun)
        ).scalar_one() == 0

    def test_closes_sync_run_linked_to_terminated_scheduler_run(
        self, factory: sessionmaker[Session], db_session: Session
    ) -> None:
        """A scheduler restart must not leave its child audit run running."""
        scheduler_run_id = runs.open_run(factory, "sync", SCHEDULED)
        db_session.add(
            SyncRun(
                started_at=SCHEDULED,
                scope=RunScope.SYMBOLS,
                status=RunStatus.RUNNING,
                symbol_count=1,
                dataset_count=1,
                job_run_id=scheduler_run_id,
            )
        )
        db_session.commit()

        runs.close_orphans(factory)
        assert runs.close_orphan_sync_runs(factory) == 1
        row = db_session.execute(select(SyncRun)).scalar_one()
        assert row.status == RunStatus.FAILED
        assert row.finished_at is not None


class TestLastSuccess:
    """Seeds the gauge at start-up; without it a restart would leave it at
    zero and `JobOverdue` would fire on a healthy system."""

    def test_it_reports_the_newest_success_per_job(
        self, factory: sessionmaker[Session], db_session: Session
    ) -> None:
        for days, job in ((3, "sync"), (1, "sync"), (2, "market")):
            run_id = runs.open_run(factory, job, SCHEDULED - timedelta(days=days))
            runs.close_run(factory, run_id, result="ok", exit_code=0)
        found = runs.last_success(factory)
        assert set(found) == {"sync", "market"}

    def test_a_failure_is_not_a_success(
        self, factory: sessionmaker[Session], db_session: Session
    ) -> None:
        run_id = runs.open_run(factory, "sync", SCHEDULED)
        runs.close_run(factory, run_id, result="failed", exit_code=1)
        assert runs.last_success(factory) == {}

    def test_a_locked_run_is_not_a_success_either(
        self, factory: sessionmaker[Session], db_session: Session
    ) -> None:
        """`locked` means the previous run was still going. Nothing was
        refreshed, so the freshness clock must not move."""
        run_id = runs.open_run(factory, "sync", SCHEDULED)
        runs.close_run(factory, run_id, result="locked", exit_code=4)
        assert runs.last_success(factory) == {}

    def test_an_unrun_job_is_absent_rather_than_zero(
        self, factory: sessionmaker[Session]
    ) -> None:
        assert "prune" not in runs.last_success(factory)
