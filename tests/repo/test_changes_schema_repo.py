"""The change outbox against a real database.

`Xid8Type` exists because neither side of the driver handles `xid8` alone,
and both halves of that claim need a server to be true or false. The unit
tests cover the compiled SQL; these cover what PostgreSQL and psycopg
actually do with it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from yfin.models.changes import PipelineOutbox, PipelineRelayOffset

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
