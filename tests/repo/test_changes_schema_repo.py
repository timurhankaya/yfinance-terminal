"""The change outbox against a real database.

`Xid8Type` exists because neither side of the driver handles `xid8` alone; the
unit tests cover the compiled SQL, these cover what the server and psycopg do.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from yfin.models.changes import PipelineOutbox, PipelineRelayOffset
from yfin.storage.changes import ChangeCollector, ChangeContext

pytestmark = pytest.mark.repo


def _queue(session: Session, *, family: str = "fundamentals", key: str = "AAPL") -> None:
    session.execute(
        text(
            "INSERT INTO pipeline_outbox (created_at, family, partition_key, payload) "
            "VALUES (now(), :f, :k, :p)"
        ),
        {"f": family, "k": key, "p": '{"v":1}'},
    )


def test_xid_defaults_to_the_writing_transaction(db_session: Session) -> None:
    """The column takes `pg_current_xact_id()`, which the data writes ahead
    of the flush have already assigned -- so the default costs nothing."""
    _queue(db_session)
    row = db_session.execute(
        select(PipelineOutbox.xid).order_by(PipelineOutbox.id.desc()).limit(1)
    ).scalar_one()
    current = db_session.execute(text("SELECT pg_current_xact_id()::text")).scalar_one()
    assert row == int(current)


def test_an_xid_comes_back_as_an_int(db_session: Session) -> None:
    """psycopg returns xid8 as text; the relay orders by it, and as text
    '9' sorts after '10'."""
    _queue(db_session)
    value = db_session.execute(
        select(PipelineOutbox.xid).order_by(PipelineOutbox.id.desc()).limit(1)
    ).scalar_one()
    assert isinstance(value, int)


def test_an_xid_can_be_bound_as_a_parameter(db_session: Session) -> None:
    """There is no implicit `bigint -> xid8` cast, so the type has to put
    one in the SQL. Without it this comparison raises 42883."""
    _queue(db_session)
    current = int(db_session.execute(text("SELECT pg_current_xact_id()::text")).scalar_one())
    found = db_session.execute(
        select(PipelineOutbox.id).where(PipelineOutbox.xid == current)
    ).scalars().all()
    assert found


def test_rows_of_one_transaction_share_an_xid(db_session: Session) -> None:
    """The relay reads a transaction as a unit: it may not publish half of
    one and then wait, which is why the cursor is the pair (xid, id)."""
    for _ in range(3):
        _queue(db_session)
    xids = db_session.execute(
        select(PipelineOutbox.xid).order_by(PipelineOutbox.id.desc()).limit(3)
    ).scalars().all()
    assert len(set(xids)) == 1


def test_the_offset_table_starts_empty(db_session: Session) -> None:
    """No migration in this repository writes data, and the relay inserts
    its own row on first use. Seeding it would give the table two creation
    paths -- and the repo fixtures build the schema from `Base.metadata`
    rather than by running migrations, so a seeded row would exist in
    production and not here."""
    assert db_session.execute(select(PipelineRelayOffset.id)).scalars().all() == []


def test_a_second_offset_row_is_refused(db_session: Session) -> None:
    """One row, enforced. The real protection against two relay processes is
    the `yfin_pipeline_relay` advisory lock; this stops the table itself
    from being able to hold two disagreeing cursors."""
    from sqlalchemy.exc import IntegrityError

    db_session.execute(
        text("INSERT INTO pipeline_relay_offset (id, updated_at) VALUES (1, now())")
    )
    with pytest.raises(IntegrityError):
        db_session.execute(
            text("INSERT INTO pipeline_relay_offset (id, updated_at) VALUES (2, now())")
        )
        db_session.flush()


def test_a_new_offset_row_starts_at_zero(db_session: Session) -> None:
    """`'0'::xid8` is below every real transaction id, so a relay starting
    on an existing outbox publishes everything rather than skipping it."""
    db_session.execute(
        text("INSERT INTO pipeline_relay_offset (id, updated_at) VALUES (1, now())")
    )
    row = db_session.execute(
        select(PipelineRelayOffset.last_published_xid, PipelineRelayOffset.last_published_id)
    ).one()
    assert row == (0, 0)


class TestFlush:
    """`flush` is the only collector method that needs a server."""

    def _collector(self) -> ChangeCollector:
        return ChangeCollector(ChangeContext(run_id=99, range_threshold=1000))

    def test_events_land_in_the_outbox(self, db_session: Session) -> None:
        collector = self._collector()
        collector.enter_dataset("news")
        collector.record("news", "insert", {"news_id": "a"}, {"news_id": "a"})
        collector.record("symbols", "update", {"symbol": "AAPL"}, {"symbol": "AAPL"})

        assert collector.flush(db_session) == 2
        rows = db_session.execute(
            select(PipelineOutbox.family, PipelineOutbox.partition_key).order_by(
                PipelineOutbox.id
            )
        ).all()
        assert rows == [("news", "a"), ("reference", "AAPL")]

    def test_the_flush_empties_the_collector(self, db_session: Session) -> None:
        """Otherwise a retry after a serialisation failure would write the
        first attempt's events a second time."""
        collector = self._collector()
        collector.record("symbols", "insert", {"symbol": "AAPL"}, {"symbol": "AAPL"})
        collector.flush(db_session)
        assert collector.pending == []
        assert collector.flush(db_session) == 0

    def test_an_empty_collector_writes_nothing(self, db_session: Session) -> None:
        before = db_session.execute(
            select(func.count()).select_from(PipelineOutbox)
        ).scalar_one()
        assert self._collector().flush(db_session) == 0
        after = db_session.execute(
            select(func.count()).select_from(PipelineOutbox)
        ).scalar_one()
        assert after == before

    def test_occurred_at_matches_created_at(self, db_session: Session) -> None:
        """Both come from one `clock_timestamp()`, so a consumer ordering by
        the envelope agrees with the relay ordering by the row."""
        collector = self._collector()
        collector.record("symbols", "insert", {"symbol": "AAPL"}, {"symbol": "AAPL"})
        collector.flush(db_session)
        created_at, payload = db_session.execute(
            select(PipelineOutbox.created_at, PipelineOutbox.payload)
            .order_by(PipelineOutbox.id.desc())
            .limit(1)
        ).one()
        assert json.loads(payload)["occurred_at"] == created_at.isoformat()

    def test_the_timestamp_is_the_statement_clock_not_the_transaction_start(
        self, db_session: Session
    ) -> None:
        """`now()` is the transaction start time, so every event of a long
        symbol transaction would claim to predate the writes it describes."""
        started = db_session.execute(text("SELECT now()")).scalar_one()
        collector = self._collector()
        collector.record("symbols", "insert", {"symbol": "AAPL"}, {"symbol": "AAPL"})
        collector.flush(db_session)
        created_at = db_session.execute(
            select(PipelineOutbox.created_at).order_by(PipelineOutbox.id.desc()).limit(1)
        ).scalar_one()
        assert created_at > started

    def test_a_payload_with_a_tab_survives_the_copy(self, db_session: Session) -> None:
        """The payload is one COPY field; an unescaped tab would shift every
        following column by one and corrupt the row."""
        collector = self._collector()
        collector.record(
            "news", "insert", {"news_id": "a"}, {"news_id": "a", "title": "x\ty\nz"}
        )
        collector.flush(db_session)
        payload = db_session.execute(
            select(PipelineOutbox.payload).order_by(PipelineOutbox.id.desc()).limit(1)
        ).scalar_one()
        assert json.loads(payload)["row"]["title"] == "x\ty\nz"


def test_the_outbox_is_a_hypertable(db_session: Session) -> None:
    """Without this the relay's `drop_chunks` cleanup has nothing to drop,
    and the queue would need DELETE plus autovacuum to keep up."""
    found = db_session.execute(
        text(
            "SELECT count(*) FROM timescaledb_information.hypertables "
            " WHERE hypertable_name = 'pipeline_outbox'"
            "   AND hypertable_schema = current_schema()"
        )
    ).scalar_one()
    assert found == 1
