"""Optional Kafka producer, behind an import guard.

`confluent-kafka` is an extra. This module is importable without it; only
building a producer requires it, and then the failure is loud and immediate
rather than a stream that quietly publishes nothing.

Topic layout is the part worth arguing about, and it is settled here:
**topic per route, partition key per row.** For ticks the route is the
exchange and the key is the symbol; a topic per symbol would be thousands
of topics and would take the broker's metadata down with it, and a single
topic would give up per-exchange isolation. Keying on the symbol is what
makes ordering per-symbol rather than per-topic. The pipeline's change
outbox uses the same shape with a different vocabulary -- see
`spec.py`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from yfin.core.logging_setup import get_logger
from yfin.outbox.spec import OutboxSpec

log = get_logger(__name__)

#: Kafka accepts [a-zA-Z0-9._-] in topic names, up to 249 characters.
_TOPIC_SAFE: Final = re.compile(r"[^a-zA-Z0-9._-]")
_TOPIC_MAX: Final = 249

#: Where an unknown exchange lands. The same label the connection topology
#: uses, deliberately: two spellings of "unknown" would split one stream
#: into two places for no reason.
UNKNOWN_EXCHANGE: Final = "unknown"

#: The consumer's dedupe key on outboxes that ask for one.
OUTBOX_ID_HEADER: Final = "yfin-outbox-id"


class KafkaUnavailable(RuntimeError):
    """Kafka is enabled but the extra is not installed, or is misconfigured."""


@dataclass(frozen=True)
class OutboxMessage:
    """One row on its way to a topic."""

    id: int
    #: The writing transaction, for the outboxes whose relay walks it.
    #: `None` where the cursor is the row id.
    xid: int | None
    #: Becomes the Kafka message key: the unit ordering holds within.
    key: str
    #: Picks the topic. `None` and blank both fall back to the unknown label.
    route: str | None
    payload: str


def topic_for(
    pattern: str, route: str | None, *, placeholder: str, upper_case: bool
) -> str:
    """Renders a topic name and makes it legal.

    Sanitising rather than trusting the route value: an exchange code comes
    from `symbols.exchange`, which discovery paths populate, so a value with
    a slash or a space is not impossible. An illegal name would fail at
    produce time, one message at a time.
    """
    label = (route or "").strip()
    if upper_case:
        label = label.upper()
    name = pattern.replace(placeholder, label or UNKNOWN_EXCHANGE)
    # `_` next to `.` triggers Kafka's metric-collision warning, so the
    # separator is normalised to `-`.
    name = _TOPIC_SAFE.sub("-", name).replace("._", ".-").replace("_.", "-.")
    return name[:_TOPIC_MAX]


def topic_for_spec(spec: OutboxSpec, route: str | None) -> str:
    """`topic_for` with the naming rules the spec already carries."""
    return topic_for(
        spec.topic_pattern,
        route,
        placeholder=spec.placeholder,
        upper_case=spec.upper_case_route,
    )


class Producer(Protocol):
    """The slice of confluent_kafka.Producer the relay uses.

    `headers` is optional because only one outbox sends any, and because a
    fake producer in a test should not have to accept an argument the tick
    path never passes.
    """

    def produce(
        self,
        topic: str,
        value: bytes,
        key: bytes,
        *,
        # Spelled as confluent_kafka spells it. A narrower `list[tuple[str,
        # bytes]]` would not be satisfied by the real producer: a list is
        # invariant, so the two would simply fail to match.
        headers: list[tuple[str, str | bytes | None]] | None = ...,
    ) -> None: ...
    def flush(self, timeout: float = ...) -> int: ...


def build_producer(bootstrap_servers: str, *, client_id: str) -> Producer:
    """A producer configured so per-key ordering actually holds.

    The two settings below are not tuning. librdkafka defaults allow more
    than one in-flight request per connection with retries enabled, and a
    retried batch can then land after a later one -- reordering messages
    within the partition that per-key ordering depends on.
    `enable.idempotence` bounds in-flight requests and de-duplicates
    retries; it also implies acks=all, which is stated here rather than
    left implicit.

    `client.id` comes from the spec so the two relays are told apart in the
    broker's own logs rather than both reporting as one client.
    """
    if not bootstrap_servers:
        raise KafkaUnavailable(
            "yf_kafka_enabled is on but yf_kafka_bootstrap_servers is empty"
        )
    try:
        from confluent_kafka import Producer as ConfluentProducer
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise KafkaUnavailable(
            "yf_kafka_enabled is on but confluent-kafka is not installed; "
            'install the extra: pip install "yfin[kafka]"'
        ) from exc

    return ConfluentProducer(
        {
            "bootstrap.servers": bootstrap_servers,
            "enable.idempotence": True,
            "acks": "all",
            "client.id": client_id,
        }
    )


def existing_topics(bootstrap_servers: str, timeout: float = 10.0) -> set[str]:
    """Topic names the broker already knows.

    The relay checks these at start rather than relying on
    `auto.create.topics.enable`: a topic created implicitly gets the
    broker's default partition count, and the wrong partition count
    silently costs per-key ordering.
    """
    try:
        from confluent_kafka.admin import AdminClient
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise KafkaUnavailable("confluent-kafka is not installed") from exc

    admin = AdminClient({"bootstrap.servers": bootstrap_servers})
    metadata = admin.list_topics(timeout=timeout)
    return set(metadata.topics)


class DeliveryTracker:
    """Counts how a produce batch actually landed.

    The relay may not advance its offset until every message in the batch
    is acknowledged. Without that, a broker outage mid-batch would move the
    offset past messages nobody received -- and the outbox is then the only
    place they existed.
    """

    def __init__(self) -> None:
        self.delivered = 0
        self.failed: list[str] = []

    def callback(self, error: Any, _message: Any) -> None:
        if error is not None:
            self.failed.append(str(error))
        else:
            self.delivered += 1

    @property
    def ok(self) -> bool:
        return not self.failed


def publish(
    producer: Producer,
    messages: Sequence[OutboxMessage],
    *,
    spec: OutboxSpec,
    flush_timeout: float = 30.0,
) -> DeliveryTracker:
    """Publishes a batch and waits for every acknowledgement."""
    tracker = DeliveryTracker()
    for message in messages:
        extra: dict[str, Any] = {}
        if spec.id_header:
            extra["headers"] = [(OUTBOX_ID_HEADER, str(message.id).encode("utf-8"))]
        producer.produce(  # type: ignore[call-arg]
            topic_for_spec(spec, message.route),
            value=message.payload.encode("utf-8"),
            key=message.key.encode("utf-8"),
            on_delivery=tracker.callback,
            **extra,
        )
    remaining = producer.flush(flush_timeout)
    if remaining:
        tracker.failed.append(f"{remaining} message(s) still queued after flush")
    return tracker


__all__ = [
    "OUTBOX_ID_HEADER",
    "UNKNOWN_EXCHANGE",
    "DeliveryTracker",
    "KafkaUnavailable",
    "OutboxMessage",
    "Producer",
    "build_producer",
    "existing_topics",
    "publish",
    "topic_for",
    "topic_for_spec",
]
