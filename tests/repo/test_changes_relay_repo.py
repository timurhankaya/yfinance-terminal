"""The `xid` cursor, against transactions that really commit.

These cannot use `db_session`. Its outer transaction is never committed, so
nothing it writes ever falls below `pg_snapshot_xmin` and the cursor would
correctly refuse to publish any of it. Each test here opens its own
connections from the test engine, commits for real, and truncates what it
used.

What is under test is the one failure the cursor exists to prevent: a
transaction that takes its outbox ids early and commits late must be waited
for, not stepped over. Its rows exist nowhere else.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from yfin.outbox.cursor import Position, XidCursor
from yfin.outbox.relay import OutboxRelay, RelayConfig, relay_lag
from yfin.outbox.spec import CHANGES_OUTBOX

pytestmark = pytest.mark.repo


class FakeProducer:
    def __init__(self) -> None:
        self.produced: list[tuple[str, bytes, bytes]] = []

    def produce(
        self,
        topic: str,
        value: bytes,
        key: bytes,
        on_delivery: Any,
        headers: Any = None,
    ) -> None:
        self.produced.append((topic, value, key))
        on_delivery(None, None)

    def flush(self, timeout: float = 0.0) -> int:
        return 0


@pytest.fixture
def factory(test_engine: Engine) -> Iterator[sessionmaker[Session]]:
    """Real sessions on the test engine; the tables are cleared afterwards.

    The warm-up write is not decoration. `pipeline_outbox` is a hypertable,
    and the FIRST insert into an empty one CREATES the chunk and holds a
    lock on it until it commits. The tests below deliberately leave a
    transaction open while a second one writes, so without an existing
    chunk the second would block on the first and the suite would hang
    rather than fail. One committed row, then deleted, leaves the chunk
    behind and the tables empty.

    `lock_timeout` turns any remaining contention into a fast failure
    instead of a hang.
    """
    with test_engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO pipeline_outbox "
                "  (created_at, family, partition_key, payload) "
                "VALUES (clock_timestamp(), 'reference', 'WARMUP', '{}')"
            )
        )
        conn.commit()
        conn.execute(text("DELETE FROM pipeline_outbox"))
        conn.commit()

    yield sessionmaker(bind=test_engine, expire_on_commit=False)

    with test_engine.connect() as conn:
        conn.execute(text("SET lock_timeout = '5s'"))
        conn.execute(text("DELETE FROM pipeline_outbox"))
        conn.execute(text("DELETE FROM pipeline_relay_offset"))
        conn.commit()


def _queue(session: Session, family: str, key: str) -> None:
    session.execute(text("SET LOCAL lock_timeout = '5s'"))
    session.execute(
        text(
            "INSERT INTO pipeline_outbox (created_at, family, partition_key, payload) "
            "VALUES (clock_timestamp(), :f, :k, :p)"
        ),
        {"f": family, "k": key, "p": '{"v":1}'},
    )


def _relay(factory: sessionmaker[Session], batch: int = 1000) -> OutboxRelay:
    return OutboxRelay(
        factory,
        RelayConfig(bootstrap_servers="unused:9092", batch_size=batch),
        CHANGES_OUTBOX,
    )


def test_a_committed_transaction_is_published(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        _queue(session, "reference", "AAPL")
        session.commit()
    producer = FakeProducer()
    assert _relay(factory).publish_once(producer) == 1
    assert producer.produced[0][0] == "yfin.changes.reference"
    assert producer.produced[0][2] == b"AAPL"


def test_an_uncommitted_transaction_is_not_published(
    factory: sessionmaker[Session],
) -> None:
    """The rows are invisible AND the xid is at or above xmin; either alone
    would be enough, and the relay must not advance past them."""
    holder = factory()
    _queue(holder, "reference", "AAPL")
    holder.flush()
    try:
        assert _relay(factory).publish_once(FakeProducer()) == 0
    finally:
        holder.rollback()
        holder.close()


def test_a_later_transaction_is_held_back_by_an_earlier_open_one(
    factory: sessionmaker[Session],
) -> None:
    """The failure the whole cursor exists to prevent.

    The first transaction takes its outbox ids first and commits LAST. An
    `id`-ordered walk would publish the second and move the offset past the
    first, and the first's rows exist nowhere else.
    """
    first = factory()
    _queue(first, "reference", "FIRST")
    first.flush()  # the xid is assigned here, before the second even starts

    with factory() as second:
        _queue(second, "reference", "SECOND")
        second.commit()

    producer = FakeProducer()
    try:
        # SECOND is committed, but FIRST is still open and holds a lower
        # xid, so xmin has not moved past it: nothing is eligible.
        assert _relay(factory).publish_once(producer) == 0
        first.commit()
    finally:
        first.close()

    assert _relay(factory).publish_once(producer) == 2
    assert [key for _, _, key in producer.produced] == [b"FIRST", b"SECOND"]


def test_a_transaction_larger_than_a_batch_is_drained_across_passes(
    factory: sessionmaker[Session],
) -> None:
    """The cursor is the PAIR for this reason: a bare xid could neither
    advance past a transaction bigger than one batch nor resume inside it,
    and one large backfill would stall the relay for good."""
    with factory() as session:
        for i in range(5):
            _queue(session, "reference", f"S{i}")
        session.commit()

    relay = _relay(factory, batch=2)
    producer = FakeProducer()
    assert relay.publish_once(producer) == 2
    assert relay.publish_once(producer) == 2
    assert relay.publish_once(producer) == 1
    assert relay.publish_once(producer) == 0
    assert [key for _, _, key in producer.produced] == [
        b"S0",
        b"S1",
        b"S2",
        b"S3",
        b"S4",
    ]


def test_the_offset_stores_both_halves(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        _queue(session, "reference", "AAPL")
        session.commit()
    _relay(factory).publish_once(FakeProducer())

    with factory() as session:
        at = XidCursor().read(session, CHANGES_OUTBOX)
    assert at.xid > 0
    assert at.id > 0


def test_a_delivery_failure_does_not_advance_the_offset(
    factory: sessionmaker[Session],
) -> None:
    class Failing(FakeProducer):
        def produce(self, topic, value, key, on_delivery, headers=None) -> None:  # type: ignore[no-untyped-def]
            on_delivery("broker down", None)

    with factory() as session:
        _queue(session, "reference", "AAPL")
        session.commit()

    relay = _relay(factory)
    assert relay.publish_once(Failing()) == 0
    with factory() as session:
        assert XidCursor().read(session, CHANGES_OUTBOX) == Position(xid=0, id=0)
    assert relay.publish_once(FakeProducer()) == 1


def test_the_dedupe_header_travels(factory: sessionmaker[Session]) -> None:
    """One transaction can write the same row twice, so the consumer's
    dedupe key is the outbox id rather than (table, key, occurred_at)."""
    captured: list[Any] = []

    class Capturing(FakeProducer):
        def produce(self, topic, value, key, on_delivery, headers=None) -> None:  # type: ignore[no-untyped-def]
            captured.append(headers)
            on_delivery(None, None)

    with factory() as session:
        _queue(session, "reference", "AAPL")
        session.commit()
    _relay(factory).publish_once(Capturing())
    assert captured and captured[0][0][0] == "yfin-outbox-id"


class TestLag:
    def test_it_counts_what_is_unpublished(self, factory: sessionmaker[Session]) -> None:
        with factory() as session:
            _queue(session, "reference", "AAPL")
            _queue(session, "news", "n1")
            session.commit()
        assert relay_lag(factory, CHANGES_OUTBOX).rows == 2
        _relay(factory).publish_once(FakeProducer())
        assert relay_lag(factory, CHANGES_OUTBOX).rows == 0

    def test_an_open_writer_is_reported_separately(
        self, factory: sessionmaker[Session]
    ) -> None:
        """A backlog with this set is not a relay problem: it cannot pass an
        open writing transaction, and the operator's fix is different."""
        holder = factory()
        _queue(holder, "reference", "AAPL")
        holder.flush()
        try:
            lag = relay_lag(factory, CHANGES_OUTBOX)
            assert lag.held_back_seconds is not None
        finally:
            holder.rollback()
            holder.close()
