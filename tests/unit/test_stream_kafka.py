"""Topic naming, delivery tracking and the publish contract.

No broker here: what these cover is the logic that decides where a
message goes and whether the relay is allowed to move on. The end-to-end
path is exercised in tests/repo against a real broker.
"""

from __future__ import annotations

import pytest

from yfin.stream.kafka import (
    UNKNOWN_EXCHANGE,
    DeliveryTracker,
    KafkaUnavailable,
    OutboxMessage,
    build_producer,
    publish,
    topic_for,
)


class FakeProducer:
    """Records what would be produced, and how it is acknowledged."""

    def __init__(self, *, fail: bool = False, stuck: int = 0) -> None:
        self.produced: list[tuple[str, bytes, bytes]] = []
        self._fail = fail
        self._stuck = stuck

    def produce(self, topic: str, value: bytes, key: bytes, on_delivery: object) -> None:
        self.produced.append((topic, value, key))
        callback = on_delivery
        assert callable(callback)
        callback("broker down" if self._fail else None, None)

    def flush(self, timeout: float = 0.0) -> int:
        return self._stuck


def _message(symbol: str = "AAPL", exchange: str | None = "NMS") -> OutboxMessage:
    return OutboxMessage(id=1, symbol=symbol, exchange=exchange, payload='{"a":1}')


# --- topic naming ----------------------------------------------------------


def test_topic_is_per_exchange() -> None:
    """A topic per symbol would be thousands of topics; a single topic
    would give up per-exchange isolation."""
    assert topic_for("yfin.ticks.{exchange}", "NMS") == "yfin.ticks.NMS"


def test_topic_upper_cases_the_exchange() -> None:
    """Raw case differences would split one topic in two and quietly halve
    the per-symbol ordering guarantee."""
    assert topic_for("yfin.ticks.{exchange}", "nms") == "yfin.ticks.NMS"


@pytest.mark.parametrize("exchange", [None, "", "   "])
def test_missing_exchange_uses_the_unknown_label(exchange: str | None) -> None:
    assert topic_for("yfin.ticks.{exchange}", exchange) == f"yfin.ticks.{UNKNOWN_EXCHANGE}"


def test_illegal_characters_are_replaced() -> None:
    """Exchange codes come from discovery paths, so a slash is not
    impossible -- and an illegal name fails one message at a time."""
    assert topic_for("yfin.ticks.{exchange}", "A/B C") == "yfin.ticks.A-B-C"


def test_topic_name_is_length_capped() -> None:
    assert len(topic_for("yfin.ticks.{exchange}", "X" * 400)) <= 249


def test_a_pattern_without_the_placeholder_gives_one_topic() -> None:
    """Operators who want a single topic just leave {exchange} out."""
    assert topic_for("yfin.ticks", "NMS") == "yfin.ticks"


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
        topic_pattern="yfin.ticks.{exchange}",
    )
    assert tracker.delivered == 2
    assert [p[0] for p in producer.produced] == ["yfin.ticks.NMS", "yfin.ticks.NMS"]


def test_publish_keys_on_the_symbol() -> None:
    """The key is what makes ordering per-symbol rather than per-topic."""
    producer = FakeProducer()
    publish(producer, [_message("AAPL")], topic_pattern="yfin.ticks.{exchange}")
    assert producer.produced[0][2] == b"AAPL"


def test_publish_reports_a_broker_failure() -> None:
    producer = FakeProducer(fail=True)
    tracker = publish(producer, [_message()], topic_pattern="yfin.ticks.{exchange}")
    assert not tracker.ok


def test_messages_left_queued_after_flush_count_as_failure() -> None:
    """flush() returning non-zero means the broker never confirmed them."""
    producer = FakeProducer(stuck=3)
    tracker = publish(producer, [_message()], topic_pattern="yfin.ticks.{exchange}")
    assert not tracker.ok
    assert "still queued" in tracker.failed[-1]


# --- configuration ---------------------------------------------------------


def test_empty_bootstrap_servers_is_refused() -> None:
    """Loud at start beats a stream that quietly publishes nothing."""
    with pytest.raises(KafkaUnavailable, match="bootstrap_servers"):
        build_producer("")


def test_producer_requests_idempotence() -> None:
    """Not tuning: librdkafka's defaults let a retried batch land after a
    later one, which reorders within the partition that per-symbol
    ordering depends on."""
    import inspect

    from yfin.stream import kafka

    source = inspect.getsource(kafka.build_producer)
    assert '"enable.idempotence": True' in source
    assert '"acks": "all"' in source
