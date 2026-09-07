"""Who creates the collector, and what happens when a transaction is replayed.

The context travels on the payload because only the runner knows the run.
The collector is built per ATTEMPT, and that distinction is the whole point
of this file: `persist_with_retry` replays the block on a lock conflict, and
a collector shared across attempts would publish the rolled-back attempt's
events alongside the committed one's.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from yfin.datasets.base import NormalizedResult
from yfin.models import Symbol
from yfin.models.changes import PipelineOutbox
from yfin.pipeline.payload import SymbolPayload
from yfin.pipeline.persist import persist_symbol, persist_with_retry
from yfin.storage.changes import ChangeContext, context_for
from yfin.storage.contracts import TableWrite

pytestmark = pytest.mark.repo

T0 = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)


class _Target:
    """The narrowest thing `persist_symbol` will accept as a dataset."""

    name = "analyst_price_targets"
    produces = ("analyst_price_targets",)

    def __init__(self, current: int) -> None:
        self._current = current

    def upsert(self, writer: Any, result: Any, *, full_refresh: bool = False) -> Any:
        from yfin.storage.contracts import WriteStats, apply_write

        stats = WriteStats()
        apply_write(writer, self._write(), stats)
        return stats

    def _write(self) -> TableWrite:
        return TableWrite(
            table="analyst_price_targets",
            rows=[
                {
                    "symbol": "AAPL",
                    "as_of_date": "2026-09-07",
                    "current": self._current,
                    "fetched_at": T0,
                }
            ],
            key_columns=("symbol", "as_of_date"),
            update_columns=("current", "fetched_at"),
        )


@pytest.fixture
def symbol(db_session: Session) -> str:
    db_session.add(Symbol(symbol="AAPL", is_active=True))
    db_session.flush()
    return "AAPL"


def _payload(current: int, changes: ChangeContext | None) -> SymbolPayload:
    payload = SymbolPayload(symbol="AAPL", resolved=True)
    payload.results = [(_Target(current), NormalizedResult(), 1, 0)]  # type: ignore[list-item]
    payload.changes = changes
    return payload


def _ctx() -> ChangeContext:
    return ChangeContext(run_id=42, range_threshold=1000)


def _queued(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(
        select(PipelineOutbox.payload).order_by(PipelineOutbox.id)
    ).scalars()
    return [json.loads(row) for row in rows]


class TestContextFor:
    """One place decides that off means nothing happens."""

    def test_off_gives_no_context(self) -> None:
        assert context_for(enabled=False, run_id=1, range_threshold=1000) is None

    def test_on_carries_the_run_and_the_threshold(self) -> None:
        ctx = context_for(enabled=True, run_id=7, range_threshold=250)
        assert ctx == ChangeContext(run_id=7, range_threshold=250)


class TestPersistSymbol:
    def test_the_events_reach_the_outbox(self, db_session: Session, symbol: str) -> None:
        from yfin.storage.changes import ChangeCollector

        collector = ChangeCollector(_ctx())
        persist_symbol(db_session, _payload(1, _ctx()), collector)
        queued = _queued(db_session)
        assert [e["op"] for e in queued] == ["insert"]
        assert queued[0]["run_id"] == 42

    def test_the_dataset_is_named(self, db_session: Session, symbol: str) -> None:
        """Six datasets write `symbols`; the table alone cannot say which
        fetch produced a row."""
        from yfin.storage.changes import ChangeCollector

        collector = ChangeCollector(_ctx())
        persist_symbol(db_session, _payload(1, _ctx()), collector)
        assert _queued(db_session)[0]["dataset"] == "analyst_price_targets"

    def test_without_a_context_nothing_is_queued(
        self, db_session: Session, symbol: str
    ) -> None:
        persist_symbol(db_session, _payload(1, None), None)
        assert _queued(db_session) == []


class TestRetry:
    def test_a_replayed_transaction_publishes_one_set(
        self, db_session: Session, symbol: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reason the collector is built per attempt.

        The first attempt writes its rows and its events, then fails; the
        rollback takes both. A collector shared across attempts would carry
        the first attempt's events into the second and publish a write that
        never committed.
        """
        factory = sessionmaker(
            bind=db_session.connection(),
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        calls = {"n": 0}
        real = persist_symbol

        def flaky(session: Session, payload: SymbolPayload, collector: Any = None) -> Any:
            calls["n"] += 1
            records = real(session, payload, collector)
            if calls["n"] == 1:
                raise _LockConflict()
            return records

        monkeypatch.setattr("yfin.pipeline.persist.persist_symbol", flaky)
        persist_with_retry(factory, _payload(1, _ctx()), attempts=3)

        assert calls["n"] == 2
        queued = _queued(db_session)
        assert [e["op"] for e in queued] == ["insert"]

    def test_a_failing_transaction_publishes_nothing(
        self, db_session: Session, symbol: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Atomic with the data: a failed commit loses both."""
        factory = sessionmaker(
            bind=db_session.connection(),
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        real = persist_symbol

        def always_fails(
            session: Session, payload: SymbolPayload, collector: Any = None
        ) -> Any:
            real(session, payload, collector)
            raise RuntimeError("write blew up")

        monkeypatch.setattr("yfin.pipeline.persist.persist_symbol", always_fails)
        persist_with_retry(factory, _payload(1, _ctx()), attempts=1)

        assert db_session.execute(
            select(func.count()).select_from(PipelineOutbox)
        ).scalar_one() == 0


class _LockConflict(Exception):
    """Carries the SQLSTATE `is_lock_conflict` reads."""

    def __init__(self) -> None:
        super().__init__("serialization failure")
        self.orig = type("Orig", (), {"sqlstate": "40001"})()
