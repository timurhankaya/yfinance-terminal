"""A shard's counters reaching `run_metrics`.

The write is outside the symbol transactions and swallows its own errors:
observability that can fail a run is worth less than the run.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from yfin.core.metrics import Accumulator
from yfin.models import RunStatus, SyncRun
from yfin.models.ops import RunMetric
from yfin.pipeline import run_metrics

pytestmark = pytest.mark.repo


@pytest.fixture
def factory(db_session: Session) -> sessionmaker[Session]:
    return sessionmaker(
        bind=db_session.connection(),
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )


@pytest.fixture
def run_id(db_session: Session) -> int:
    run = SyncRun(started_at=datetime.now(UTC), status=RunStatus.RUNNING)
    db_session.add(run)
    db_session.flush()
    return int(run.id)


def _filled() -> Accumulator:
    acc = Accumulator()
    acc.inc("yfin_sync_yahoo_requests_total", 2, dataset="history", outcome="ok")
    acc.inc("yfin_sync_retries_total", kind="lock_conflict")
    return acc


def _rows(session: Session) -> dict[tuple[str, str], int]:
    return {
        (r.name, r.labels): r.value
        for r in session.execute(select(RunMetric)).scalars()
    }


def test_the_counters_land(
    factory: sessionmaker[Session], db_session: Session, run_id: int
) -> None:
    assert run_metrics.flush(factory, run_id, 0, _filled()) == 2
    assert _rows(db_session) == {
        ("yfin_sync_yahoo_requests_total", '{"dataset":"history","outcome":"ok"}'): 2,
        ("yfin_sync_retries_total", '{"kind":"lock_conflict"}'): 1,
    }


def test_two_shards_write_side_by_side(
    factory: sessionmaker[Session], db_session: Session, run_id: int
) -> None:
    """Shards are separate `spawn` processes, so the parent cannot see a
    child's memory: each writes its own rows and the exporter sums them."""
    run_metrics.flush(factory, run_id, 0, _filled())
    run_metrics.flush(factory, run_id, 1, _filled())
    total = db_session.execute(
        select(func.sum(RunMetric.value)).where(
            RunMetric.name == "yfin_sync_retries_total"
        )
    ).scalar_one()
    assert total == 2
    assert db_session.execute(
        select(func.count()).select_from(RunMetric)
    ).scalar_one() == 4


def test_a_second_flush_overwrites_rather_than_failing(
    factory: sessionmaker[Session], db_session: Session, run_id: int
) -> None:
    """A primary-key violation at the very end of a successful run would be
    the worst possible moment to fail."""
    run_metrics.flush(factory, run_id, 0, _filled())
    acc = _filled()
    acc.inc("yfin_sync_retries_total", 5, kind="lock_conflict")
    run_metrics.flush(factory, run_id, 0, acc)
    assert _rows(db_session)[("yfin_sync_retries_total", '{"kind":"lock_conflict"}')] == 6


def test_an_empty_accumulator_writes_nothing(
    factory: sessionmaker[Session], db_session: Session, run_id: int
) -> None:
    assert run_metrics.flush(factory, run_id, 0, Accumulator()) == 0
    assert _rows(db_session) == {}


def test_no_accumulator_is_not_an_error(
    factory: sessionmaker[Session], run_id: int
) -> None:
    """A long-lived service counts into Prometheus and installs none."""
    assert run_metrics.flush(factory, run_id, 0, None) == 0


def test_a_write_failure_does_not_raise(
    factory: sessionmaker[Session], run_id: int
) -> None:
    """A run that succeeded must not be reported as failed because its
    counters could not be stored."""
    assert run_metrics.flush(factory, run_id + 10_000, 0, _filled()) == 0


def test_the_counters_go_when_the_run_does(
    factory: sessionmaker[Session], db_session: Session, run_id: int
) -> None:
    """CASCADE from `sync_runs`: a counter for a run nobody kept is a number
    with nothing to attach it to."""
    run_metrics.flush(factory, run_id, 0, _filled())
    db_session.execute(
        select(SyncRun).where(SyncRun.id == run_id)
    ).scalar_one()
    db_session.delete(db_session.get(SyncRun, run_id))
    db_session.flush()
    assert _rows(db_session) == {}


def test_a_no_proxy_run_flushes_its_counters(
    test_engine: Engine, monkeypatch: pytest.MonkeyPatch, cleanup_tables: list[str]
) -> None:
    """The in-process single shard is shard 0; without a flush the exporter
    would see nothing from the default `yfin sync`."""
    from yfin.core import metrics
    from yfin.pipeline import shard

    cleanup_tables.extend(["run_metrics", "sync_run_items", "sync_runs"])

    def fake_run_shard(*args: object, **kwargs: object) -> None:
        metrics.inc("yfin_sync_yahoo_requests_total", dataset="info", outcome="ok")

    monkeypatch.setattr(shard, "run_shard", fake_run_shard)
    tally = shard.run_sharded(test_engine, ["AAPL"], ["info"], no_proxy=True)

    with test_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT name FROM run_metrics WHERE run_id = :r"), {"r": tally.run_id}
        ).scalars().all()
    assert rows == ["yfin_sync_yahoo_requests_total"]
