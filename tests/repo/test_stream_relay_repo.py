"""The relay against a real database (broker faked).

What is checked here is the part that decides whether a message can be
lost: the offset must not move past anything the broker did not confirm,
and a chunk must not be dropped while it still holds unpublished rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from yfin.stream.relay import OutboxRelay, RelayConfig, relay_lag

pytestmark = pytest.mark.repo

TS = datetime(2026, 9, 7, 14, 30, tzinfo=UTC)


class FakeProducer:
    def __init__(self, *, fail: bool = False) -> None:
        self.produced: list[tuple[str, bytes, bytes]] = []
        self.fail = fail

    def produce(self, topic: str, value: bytes, key: bytes, on_delivery: Any) -> None:
        self.produced.append((topic, value, key))
        on_delivery("broker down" if self.fail else None, None)

    def flush(self, timeout: float = 0.0) -> int:
        return 0


@pytest.fixture
def factory(db_session: Session) -> sessionmaker[Session]:
    return sessionmaker(
        bind=db_session.connection(),
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )


@pytest.fixture
def relay(factory: sessionmaker[Session]) -> OutboxRelay:
    return OutboxRelay(factory, RelayConfig(bootstrap_servers="unused:9092"))


def _queue(session: Session, count: int, *, exchange: str = "NMS", start: int = 0) -> None:
    for i in range(count):
        session.execute(
            text(
                "INSERT INTO stream_outbox (created_at, symbol, exchange, payload) "
                "VALUES (:ts, :s, :e, :p)"
            ),
            {
                "ts": TS + timedelta(seconds=start + i),
                "s": "AAPL",
                "e": exchange,
                "p": f'{{"n":{start + i}}}',
            },
        )
    session.commit()


def _offset(session: Session) -> int:
    value = session.execute(
        text("SELECT last_published_id FROM stream_relay_offset WHERE id = 1")
    ).scalar_one_or_none()
    return int(value) if value is not None else -1


# --- publishing ------------------------------------------------------------


def test_publishes_the_queue_and_advances(relay: OutboxRelay, db_session: Session) -> None:
    _queue(db_session, 3)
    producer = FakeProducer()
    assert relay.publish_once(producer) == 3
    assert len(producer.produced) == 3
    assert _offset(db_session) > 0


def test_a_second_pass_publishes_nothing_new(
    relay: OutboxRelay, db_session: Session
) -> None:
    _queue(db_session, 3)
    producer = FakeProducer()
    relay.publish_once(producer)
    assert relay.publish_once(producer) == 0
    assert len(producer.produced) == 3


def test_rows_are_published_in_id_order(relay: OutboxRelay, db_session: Session) -> None:
    """The single-writer invariant makes id equal commit order; the relay
    depends on that to walk the queue without gaps."""
    _queue(db_session, 5)
    producer = FakeProducer()
    relay.publish_once(producer)
    payloads = [value.decode() for _, value, _ in producer.produced]
    assert payloads == [f'{{"n":{i}}}' for i in range(5)]


def test_batch_size_bounds_a_pass(factory: sessionmaker[Session], db_session: Session) -> None:
    _queue(db_session, 10)
    relay = OutboxRelay(
        factory, RelayConfig(bootstrap_servers="unused:9092", batch_size=4)
    )
    assert relay.publish_once(FakeProducer()) == 4


def test_topic_comes_from_the_stored_exchange(
    relay: OutboxRelay, db_session: Session
) -> None:
    _queue(db_session, 1, exchange="IST")
    producer = FakeProducer()
    relay.publish_once(producer)
    assert producer.produced[0][0] == "yfin.ticks.IST"


# --- failure ---------------------------------------------------------------


def test_a_delivery_failure_does_not_advance_the_offset(
    relay: OutboxRelay, db_session: Session
) -> None:
    """The one thing that must never happen.

    Moving the offset past a message the broker did not confirm loses it
    outright: the outbox is the only place it exists.
    """
    _queue(db_session, 3)
    assert relay.publish_once(FakeProducer(fail=True)) == 0
    assert _offset(db_session) == 0
    assert relay.stats.last_error is not None


def test_the_same_rows_are_retried_after_a_failure(
    relay: OutboxRelay, db_session: Session
) -> None:
    """At-least-once: a duplicate is acceptable, a gap is not."""
    _queue(db_session, 3)
    relay.publish_once(FakeProducer(fail=True))
    good = FakeProducer()
    assert relay.publish_once(good) == 3


# --- lag -------------------------------------------------------------------


def test_lag_counts_unpublished_rows(
    relay: OutboxRelay, factory: sessionmaker[Session], db_session: Session
) -> None:
    _queue(db_session, 4)
    pending, _ = relay_lag(factory)
    assert pending == 4
    relay.publish_once(FakeProducer())
    assert relay_lag(factory)[0] == 0


def test_lag_on_an_empty_outbox_is_zero(factory: sessionmaker[Session]) -> None:
    assert relay_lag(factory) == (0, 0)


# --- cleanup ---------------------------------------------------------------


def test_unpublished_chunks_are_not_dropped(
    relay: OutboxRelay, db_session: Session
) -> None:
    """The cutoff is the oldest UNPUBLISHED row, so a chunk still holding
    work is never removed."""
    _queue(db_session, 2)
    relay.drop_published_chunks()
    remaining = db_session.execute(
        text("SELECT count(*) FROM stream_outbox")
    ).scalar_one()
    assert remaining == 2


def test_cleanup_is_safe_with_no_offset_row(
    factory: sessionmaker[Session], db_session: Session
) -> None:
    db_session.execute(text("DELETE FROM stream_relay_offset"))
    db_session.commit()
    relay = OutboxRelay(factory, RelayConfig(bootstrap_servers="unused:9092"))
    assert relay.drop_published_chunks() == 0


def test_offset_row_is_created_on_first_use(
    relay: OutboxRelay, db_session: Session
) -> None:
    db_session.execute(text("DELETE FROM stream_relay_offset"))
    db_session.commit()
    _queue(db_session, 1)
    relay.publish_once(FakeProducer())
    assert _offset(db_session) > 0
