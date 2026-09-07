"""Pruning the run audit, and the two things it must not do.

Nothing pruned these tables before, so they grew without bound -- and they
are not idle: the freshness query walks `sync_run_items` every five minutes
and `scheduler_runs` backs the job dashboard.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from yfin.models import RunStatus, SyncRun, SyncRunItem
from yfin.models.ops import RunMetric, SchedulerRun
from yfin.pipeline.prune import prune_audit, run_prune

pytestmark = pytest.mark.repo

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
CUTOFF = NOW - timedelta(days=30)


def _run(session: Session, *, age_days: int, finished: bool = True) -> int:
    started = NOW - timedelta(days=age_days)
    run = SyncRun(
        started_at=started,
        status=RunStatus.OK if finished else RunStatus.RUNNING,
        finished_at=started + timedelta(minutes=5) if finished else None,
    )
    session.add(run)
    session.flush()
    session.add(
        SyncRunItem(run_id=run.id, symbol="AAPL", dataset="history", status="ok")
    )
    session.add(
        RunMetric(
            run_id=run.id,
            shard_index=0,
            name="yfin_sync_retries_total",
            labels='{"kind":"timeout"}',
            value=3,
        )
    )
    session.flush()
    return int(run.id)


def _scheduler_row(session: Session, *, age_days: int, finished: bool = True) -> None:
    at = NOW - timedelta(days=age_days)
    session.add(
        SchedulerRun(
            job="sync",
            scheduled_at=at,
            started_at=at,
            finished_at=at + timedelta(minutes=5) if finished else None,
            exit_code=0 if finished else None,
            result="ok" if finished else None,
        )
    )
    session.flush()


def _count(session: Session, model: type) -> int:
    return int(session.execute(select(func.count()).select_from(model)).scalar_one())


class TestPruneAudit:
    def test_an_old_finished_run_goes(self, db_session: Session) -> None:
        _run(db_session, age_days=60)
        assert prune_audit(db_session, CUTOFF)["sync_runs"] == 1
        assert _count(db_session, SyncRun) == 0

    def test_a_recent_run_stays(self, db_session: Session) -> None:
        _run(db_session, age_days=5)
        assert prune_audit(db_session, CUTOFF)["sync_runs"] == 0
        assert _count(db_session, SyncRun) == 1

    def test_a_running_row_is_never_deleted(self, db_session: Session) -> None:
        """Older than the window and not finished means still going, or
        interrupted. Both are things an operator has to be able to see."""
        _run(db_session, age_days=60, finished=False)
        assert prune_audit(db_session, CUTOFF)["sync_runs"] == 0
        assert _count(db_session, SyncRun) == 1

    def test_items_and_metrics_go_with_the_run(self, db_session: Session) -> None:
        """Both cascade. Deleting them explicitly would do the same work
        twice and could only disagree with the FK about what happened."""
        _run(db_session, age_days=60)
        assert _count(db_session, SyncRunItem) == 1
        assert _count(db_session, RunMetric) == 1
        prune_audit(db_session, CUTOFF)
        assert _count(db_session, SyncRunItem) == 0
        assert _count(db_session, RunMetric) == 0

    def test_scheduler_rows_are_pruned_too(self, db_session: Session) -> None:
        _scheduler_row(db_session, age_days=60)
        _scheduler_row(db_session, age_days=1)
        assert prune_audit(db_session, CUTOFF)["scheduler_runs"] == 1
        assert _count(db_session, SchedulerRun) == 1

    def test_an_unfinished_scheduler_row_stays(self, db_session: Session) -> None:
        _scheduler_row(db_session, age_days=60, finished=False)
        assert prune_audit(db_session, CUTOFF)["scheduler_runs"] == 0

    def test_a_dry_run_counts_and_deletes_nothing(self, db_session: Session) -> None:
        _run(db_session, age_days=60)
        _scheduler_row(db_session, age_days=60)
        counted = prune_audit(db_session, CUTOFF, dry_run=True)
        assert counted == {"sync_runs": 1, "scheduler_runs": 1}
        assert _count(db_session, SyncRun) == 1
        assert _count(db_session, SchedulerRun) == 1


class TestThroughRunPrune:
    def test_it_is_off_unless_asked_for(self, db_session: Session) -> None:
        """Like every other date-bounded switch: absent means untouched."""
        _run(db_session, age_days=60)
        report = run_prune(db_session, enabled=True, orphan_news=False, orphan_reports=False)
        assert report.audit == {}
        assert _count(db_session, SyncRun) == 1

    def test_it_needs_pruning_to_be_enabled(self, db_session: Session) -> None:
        """A deleted audit row cannot be recovered from any source, so it
        sits behind the same gate the other date-bounded switches do."""
        from yfin.pipeline.prune import PruneDisabledError

        with pytest.raises(PruneDisabledError):
            run_prune(db_session, enabled=False, audit_before=CUTOFF)

    def test_it_reports_what_it_removed(self, db_session: Session) -> None:
        _run(db_session, age_days=60)
        _scheduler_row(db_session, age_days=60)
        report = run_prune(
            db_session,
            enabled=True,
            orphan_news=False,
            orphan_reports=False,
            audit_before=CUTOFF,
        )
        assert report.audit == {"sync_runs": 1, "scheduler_runs": 1}
        assert report.total >= 2


class TestTheFreshnessIndex:
    def test_it_covers_the_cell_and_orders_by_run(self, db_session: Session) -> None:
        """The exporter walks one cell -- (symbol, region, dataset) -- back to
        its latest run, every five minutes, over a table that grows by
        symbols x datasets every night.

        The assertion is on the index DEFINITION rather than on a query
        plan. At this table's size PostgreSQL would rightly scan whatever
        exists, so a plan here would measure the planner and not the schema;
        what the exporter needs is that the prefix and the ordering are
        there at all. Whether the query meets its five-second budget at real
        scale is a measurement, and belongs in
        docs/measurements/observability.md.
        """
        from sqlalchemy import text

        definition = db_session.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                " WHERE schemaname = current_schema() "
                "   AND indexname = 'ix_sync_run_items_cell_run'"
            )
        ).scalar_one()
        assert "(symbol, region, dataset, run_id DESC)" in definition, definition
