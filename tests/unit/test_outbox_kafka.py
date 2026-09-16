"""Topic naming, delivery tracking and the publish contract, without a broker (the
end-to-end path lives in tests/repo). Two outboxes drive the same code: the tick spec must
produce what it did before the extraction, and only a spec asking for the dedupe header
sends it."""

from __future__ import annotations

from dataclasses import replace

import pytest

from yfin.outbox.kafka import (
    OUTBOX_ID_HEADER,
    UNKNOWN_EXCHANGE,
    DeliveryTracker,
    KafkaUnavailable,
    OutboxMessage,
    build_producer,
    publish,
    topic_for,
)
from yfin.outbox.spec import CHANGES_OUTBOX, TICK_OUTBOX


class FakeProducer:
    """Records what would be produced, and how it is acknowledged."""

    def __init__(self, *, fail: bool = False, stuck: int = 0) -> None:
        self.produced: list[tuple[str, bytes, bytes]] = []
        self.headers: list[list[tuple[str, bytes]] | None] = []
        self._fail = fail
        self._stuck = stuck

    def produce(
        self,
        topic: str,
        value: bytes,
        key: bytes,
        on_delivery: object,
        headers: list[tuple[str, bytes]] | None = None,
    ) -> None:
        self.produced.append((topic, value, key))
        self.headers.append(headers)
        callback = on_delivery
        assert callable(callback)
        callback("broker down" if self._fail else None, None)

    def flush(self, timeout: float = 0.0) -> int:
        return self._stuck


def _message(
    key: str = "AAPL", route: str | None = "NMS", *, id: int = 1
) -> OutboxMessage:
    return OutboxMessage(id=id, xid=None, key=key, route=route, payload='{"a":1}')


# --- topic naming ----------------------------------------------------------


def test_topic_is_per_exchange() -> None:
    """A topic per symbol would be thousands of topics; a single topic
    would give up per-exchange isolation."""
    assert topic_for(TICK_OUTBOX, "NMS") == "yfin.ticks.NMS"


def test_topic_upper_cases_the_exchange() -> None:
    """Raw case differences would split one topic in two and quietly halve
    the per-symbol ordering guarantee."""
    assert topic_for(TICK_OUTBOX, "nms") == "yfin.ticks.NMS"


@pytest.mark.parametrize("exchange", [None, "", "   "])
def test_missing_exchange_uses_the_unknown_label(exchange: str | None) -> None:
    assert topic_for(TICK_OUTBOX, exchange) == f"yfin.ticks.{UNKNOWN_EXCHANGE}"


def test_illegal_characters_are_replaced() -> None:
    """Exchange codes come from discovery paths, so a slash is not
    impossible -- and an illegal name fails one message at a time."""
    assert topic_for(TICK_OUTBOX, "A/B C") == "yfin.ticks.A-B-C"


def test_topic_name_is_length_capped() -> None:
    assert len(topic_for(TICK_OUTBOX, "X" * 400)) <= 249


def test_a_pattern_without_the_placeholder_gives_one_topic() -> None:
    """Operators who want a single topic just leave {exchange} out."""
    assert topic_for(replace(TICK_OUTBOX, topic_pattern="yfin.ticks"), "NMS") == (
        "yfin.ticks"
    )


# --- delivery --------------------------------------------------------------


def test_delivery_tracker_counts_successes() -> None:
    tracker = DeliveryTracker()
    tracker.callback(None, None)
    tracker.callback(None, None)
    assert tracker.delivered == 2
    assert tracker.ok


def test_delivery_tracker_records_failures() -> None:
    """The relay may not advance its offset on any failure: moving past a
    message nobody received would lose it, since the outbox is the only
    place it exists."""
    tracker = DeliveryTracker()
    tracker.callback("broker down", None)
    assert not tracker.ok
    assert tracker.failed == ["broker down"]


def test_publish_sends_one_message_per_row() -> None:
    producer = FakeProducer()
    tracker = publish(
        producer,
        [_message("AAPL"), _message("MSFT")],
        spec=TICK_OUTBOX,
    )
    assert tracker.delivered == 2
    assert [p[0] for p in producer.produced] == ["yfin.ticks.NMS", "yfin.ticks.NMS"]


def test_publish_keys_on_the_symbol() -> None:
    """The key is what makes ordering per-symbol rather than per-topic."""
    producer = FakeProducer()
    publish(producer, [_message("AAPL")], spec=TICK_OUTBOX)
    assert producer.produced[0][2] == b"AAPL"


def test_publish_reports_a_broker_failure() -> None:
    producer = FakeProducer(fail=True)
    tracker = publish(producer, [_message()], spec=TICK_OUTBOX)
    assert not tracker.ok


def test_messages_left_queued_after_flush_count_as_failure() -> None:
    """flush() returning non-zero means the broker never confirmed them."""
    producer = FakeProducer(stuck=3)
    tracker = publish(producer, [_message()], spec=TICK_OUTBOX)
    assert not tracker.ok
    assert "still queued" in tracker.failed[-1]


# --- configuration ---------------------------------------------------------


def test_empty_bootstrap_servers_is_refused() -> None:
    """Loud at start beats a stream that quietly publishes nothing."""
    with pytest.raises(KafkaUnavailable, match="bootstrap_servers"):
        build_producer("", client_id="x")


# --- the spec's vocabulary -------------------------------------------------


def test_a_family_route_is_not_upper_cased() -> None:
    """`DataFamily` is already a closed lower-case set; upper-casing it
    would name a topic no ACL and no consumer expects."""
    assert topic_for(CHANGES_OUTBOX, "fundamentals") == "yfin.changes.fundamentals"


def test_the_dedupe_header_travels_only_where_it_is_asked_for() -> None:
    """One change transaction can write the same row twice -- `symbols`
    from three datasets -- so `(table, key, occurred_at)` is not a dedupe
    key and the outbox id has to travel. Tick topics stay header-free:
    `live_ticks`' primary key is already in the payload."""
    producer = FakeProducer()
    publish(producer, [_message(id=77)], spec=CHANGES_OUTBOX)
    assert producer.headers == [[(OUTBOX_ID_HEADER, b"77")]]

    ticks = FakeProducer()
    publish(ticks, [_message(id=77)], spec=TICK_OUTBOX)
    assert ticks.headers == [None]


def test_the_key_comes_from_the_message_whatever_the_column_was() -> None:
    producer = FakeProducer()
    publish(producer, [_message("news:1", "news")], spec=CHANGES_OUTBOX)
    assert producer.produced[0][2] == b"news:1"
    assert producer.produced[0][0] == "yfin.changes.news"


def test_the_two_specs_use_different_client_ids() -> None:
    """So the broker's own logs and metrics can tell the relays apart."""
    assert TICK_OUTBOX.client_id != CHANGES_OUTBOX.client_id


# --- configuration ---------------------------------------------------------


def test_producer_requests_idempotence() -> None:
    """Not tuning: librdkafka's defaults let a retried batch land after a
    later one, which reorders within the partition that per-symbol
    ordering depends on."""
    import inspect

    from yfin.outbox import kafka

    source = inspect.getsource(kafka.build_producer)
    assert '"enable.idempotence": True' in source
    assert '"acks": "all"' in source
