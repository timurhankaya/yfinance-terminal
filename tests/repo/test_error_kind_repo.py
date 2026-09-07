"""`error_kind` and `job_run_id`: the two columns that make the audit queryable.

`error` is for a human. Grouping a dashboard by it would give one bucket per
Yahoo error string, which is one bucket per failure. `error_kind` is what a
dashboard can actually group by -- and NULL where nothing classified it,
because a guessed kind would file a failure under a cause nobody
established.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from yfin.models import ItemStatus, SyncRun, SyncRunItem
from yfin.pipeline.audit import failed_records, open_run, write_items
from yfin.pipeline.payload import SymbolPayload
from yfin.scheduler.jobs import JOB_RUN_ID_VAR

pytestmark = pytest.mark.repo


@pytest.fixture
def factory(db_session: Session) -> sessionmaker[Session]:
    return sessionmaker(
        bind=db_session.connection(),
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )


def _items(session: Session) -> list[SyncRunItem]:
    return list(
        session.execute(select(SyncRunItem).order_by(SyncRunItem.id)).scalars()
    )


class TestErrorKind:
    def test_it_reaches_the_row(
        self, factory: sessionmaker[Session], db_session: Session
    ) -> None:
        run_id = open_run(factory, symbol_count=1, dataset_count=1)
        write_items(
            factory,
            run_id,
            failed_records("AAPL", "history", "HTTPError: 429", kind="rate_limit"),
        )
        for item in _items(db_session):
            assert item.error_kind == "rate_limit"
            assert item.status is ItemStatus.FAILED

    def test_it_is_null_when_nothing_classified_it(
        self, factory: sessionmaker[Session], db_session: Session
    ) -> None:
        """Honest rather than helpful: a guessed kind would group a failure
        under a cause nobody established."""
        run_id = open_run(factory, symbol_count=1, dataset_count=1)
        write_items(factory, run_id, failed_records("AAPL", "history", "boom"))
        for item in _items(db_session):
            assert item.error_kind is None

    def test_one_row_per_table_carries_it(
        self, factory: sessionmaker[Session], db_session: Session
    ) -> None:
        """A single `table_name=NULL` row would leave "when did this table
        last fail" unanswerable, so the kind has to be on each of them.

        The dataset is found in the registry rather than named, so this
        keeps testing a MULTI-table failure as the registry changes.
        """
        from yfin.datasets import SYMBOL_DATASETS

        name = next(
            n for n in SYMBOL_DATASETS if len(SYMBOL_DATASETS[n].produces) > 1
        )
        expected = SYMBOL_DATASETS[name].produces

        run_id = open_run(factory, symbol_count=1, dataset_count=1)
        write_items(
            factory,
            run_id,
            failed_records("AAPL", name, "HTTPError: 429", kind="rate_limit"),
        )
        rows = _items(db_session)
        assert {r.table_name for r in rows} == set(expected)
        assert {r.error_kind for r in rows} == {"rate_limit"}


class TestThroughThePayload:
    def test_a_fetch_failure_carries_its_classified_kind(
        self, factory: sessionmaker[Session], db_session: Session
    ) -> None:
        from yfin.pipeline.audit import channel_records

        payload = SymbolPayload(symbol="AAPL", resolved=True)
        payload.failures.append(("history", "HTTPError: 429", "rate_limit"))
        run_id = open_run(factory, symbol_count=1, dataset_count=1)
        write_items(factory, run_id, channel_records(payload))
        assert {r.error_kind for r in _items(db_session)} == {"rate_limit"}


class TestJobRunId:
    """The join between a scheduler run and the sync it produced."""

    def test_it_comes_from_the_environment(
        self, factory: sessionmaker[Session], db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Read from the environment rather than passed in, so the join
        exists without adding a parameter to `yfin sync` that a person
        running it by hand would have to know about."""
        monkeypatch.setenv(JOB_RUN_ID_VAR, "4711")
        run_id = open_run(factory, symbol_count=1, dataset_count=1)
        run = db_session.get(SyncRun, run_id)
        assert run is not None
        assert run.job_run_id == 4711

    def test_a_manual_run_has_none(
        self, factory: sessionmaker[Session], db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NULL is the other half of the scheduled/manual split the exporter
        reports on."""
        monkeypatch.delenv(JOB_RUN_ID_VAR, raising=False)
        run_id = open_run(factory, symbol_count=1, dataset_count=1)
        run = db_session.get(SyncRun, run_id)
        assert run is not None
        assert run.job_run_id is None

    def test_a_malformed_value_is_ignored_rather_than_fatal(
        self, factory: sessionmaker[Session], db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stray variable in someone's shell should give a manual run, not
        a crash on the first line of it."""
        monkeypatch.setenv(JOB_RUN_ID_VAR, "not-a-number")
        run_id = open_run(factory, symbol_count=1, dataset_count=1)
        run = db_session.get(SyncRun, run_id)
        assert run is not None
        assert run.job_run_id is None

    def test_an_empty_value_is_treated_as_absent(
        self, factory: sessionmaker[Session], db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(JOB_RUN_ID_VAR, "")
        run_id = open_run(factory, symbol_count=1, dataset_count=1)
        run = db_session.get(SyncRun, run_id)
        assert run is not None
        assert run.job_run_id is None

    def test_no_foreign_key_ties_the_two(
        self, factory: sessionmaker[Session], db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deliberate: the scheduler and the sync are separate processes,
        and a sync must not fail because the scheduler's row was pruned."""
        monkeypatch.setenv(JOB_RUN_ID_VAR, str(2**40))
        run_id = open_run(factory, symbol_count=1, dataset_count=1)
        run = db_session.get(SyncRun, run_id)
        assert run is not None
        assert run.job_run_id == 2**40


def test_the_variable_is_the_one_the_scheduler_sets() -> None:
    """The scheduler writes this name into the subprocess's environment and
    `open_run` reads it. Two spellings would break the join in silence, with
    every scheduled run looking manual."""
    import inspect

    from yfin.scheduler.service import SchedulerService

    assert JOB_RUN_ID_VAR == "YF_JOB_RUN_ID"
    # The setter and the reader must be the same constant, not two strings
    # that happen to match today.
    assert "JOB_RUN_ID_VAR" in inspect.getsource(SchedulerService._run_job)
