"""Outbox -> Kafka, and the chunk cleanup behind it.

Its own process, so a broker outage cannot stall the writers. Delivery is
at-least-once: dying between publish and offset commit republishes, and
consumers dedupe on the row key that travels in the payload."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from yfin.core import metrics, tracing
from yfin.core.logging_setup import get_logger
from yfin.outbox.cursor import Lag, Position, cursor_for
from yfin.outbox.kafka import (
    OutboxMessage,
    Producer,
    build_producer,
    existing_topics,
    publish,
    topic_for,
)
from yfin.outbox.spec import TICK_OUTBOX, OutboxSpec

log = get_logger(__name__)


@dataclass
class RelayConfig:
    bootstrap_servers: str
    batch_size: int = 1000
    idle_sleep_seconds: float = 1.0
    #: How often to drop published chunks. Cheap, but not worth doing on
    #: every pass -- the relay runs many passes a second when busy.
    cleanup_every_seconds: float = 60.0


@dataclass
class RelayStats:
    published: int = 0
    passes: int = 0
    chunks_dropped: int = 0
    last_error: str | None = None
    failures: list[str] = field(default_factory=list)


class OutboxRelay:
    """Publishes outbox rows in id order and remembers how far it got."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        config: RelayConfig,
        spec: OutboxSpec = TICK_OUTBOX,
        *,
        producer: Producer | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._spec = spec
        self._cursor = cursor_for(spec)
        self._producer = producer
        self._stopping = threading.Event()
        self.stats = RelayStats()
        self._last_cleanup = 0.0

    # --- public API --------------------------------------------------------

    def run(self) -> RelayStats:
        """Publishes until stopped."""
        producer = self._producer or build_producer(
            self._config.bootstrap_servers, client_id=self._spec.client_id
        )
        while not self._stopping.is_set():
            published = self.publish_once(producer)
            if published == 0:
                self._sleep(self._config.idle_sleep_seconds)
            self._maybe_cleanup()
        return self.stats

    def stop(self) -> None:
        self._stopping.set()

    def verify_topics(self) -> list[str]:
        """Topics the outbox needs that the broker does not have.

        Checked rather than left to `auto.create.topics.enable`: an implicit
        topic takes the default partition count, which costs per-key ordering."""
        spec = self._spec
        with self._session_factory() as session:
            routes = (
                session.execute(
                    text(f"SELECT DISTINCT {spec.route_column} FROM {spec.table}")
                )
                .scalars()
                .all()
            )
        wanted = {topic_for(spec, route) for route in routes}
        if not wanted:
            return []
        present = existing_topics(self._config.bootstrap_servers)
        return sorted(wanted - present)

    def publish_once(self, producer: Producer) -> int:
        """One pass: read a batch, publish it, advance the offset.

        The offset moves only after every delivery is acknowledged, or an
        outage mid-batch would skip messages that exist nowhere else."""
        outbox = self._spec.table
        with self._session_factory() as session:
            at = self._cursor.read(session, self._spec)
            messages = self._cursor.batch(
                session, self._spec, at, self._config.batch_size
            )
        if not messages:
            # An empty pass is not timed and draws no span. At the relay's
            # idle poll rate that would be most of the histogram, and it
            # would pull the median to zero on exactly the graph an
            # operator reads to see whether the relay is keeping up.
            return 0

        with (
            metrics.timed("yfin_relay_pass_seconds", outbox=outbox),
            tracing.span("relay.pass", outbox=outbox, messages=len(messages)),
        ):
            return self._publish_batch(producer, messages, outbox)

    def _publish_batch(
        self, producer: Producer, messages: list[OutboxMessage], outbox: str
    ) -> int:
        tracker = publish(producer, messages, spec=self._spec)
        if not tracker.ok:
            # Deliberately not advancing: the same rows are retried on the
            # next pass. At-least-once is the contract, so a duplicate is
            # acceptable and a gap is not.
            self.stats.last_error = tracker.failed[0]
            self.stats.failures.extend(tracker.failed[:5])
            log.error(
                "kafka delivery failed; offset not advanced",
                outbox=outbox,
                delivered=tracker.delivered,
                failed=len(tracker.failed),
            )
            # A pass, not a message: the rows are retried on the next one,
            # so this counts refusals to lose them rather than losses.
            metrics.inc("yfin_relay_failures_total", outbox=outbox)
            return 0

        last = messages[-1]
        with self._session_factory() as session:
            self._cursor.advance(
                session, self._spec, Position(xid=last.xid or 0, id=last.id)
            )
        self.stats.published += len(messages)
        self.stats.passes += 1
        # After the offset moved, not before: this counts what the broker
        # acknowledged AND we recorded as done, which is the number an
        # operator compares against the consumer's own.
        metrics.inc("yfin_relay_published_total", len(messages), outbox=outbox)
        return len(messages)

    # --- cleanup -----------------------------------------------------------

    def _maybe_cleanup(self) -> None:
        now = time.monotonic()
        if now - self._last_cleanup < self._config.cleanup_every_seconds:
            return
        self._last_cleanup = now
        dropped = self.drop_published_chunks()
        self.stats.chunks_dropped += dropped
        if dropped:
            metrics.inc(
                "yfin_relay_chunks_dropped_total", dropped, outbox=self._spec.table
            )

    def drop_published_chunks(self) -> int:
        """Drops outbox chunks whose rows are all published.

        `drop_chunks` rather than DELETE: autovacuum could not keep up. The
        cutoff is the `created_at` of the oldest unpublished row, safe because
        the writer generates `created_at` in `id` order."""
        spec = self._spec
        with self._session_factory() as session:
            cutoff = self._cursor.cutoff(
                session, spec, self._cursor.read(session, spec)
            )
            if cutoff is None:
                return 0
            dropped = session.execute(
                text(f"SELECT drop_chunks('{spec.table}', older_than => :cutoff)"),
                {"cutoff": cutoff},
            ).all()
            session.commit()
        return len(dropped)

    def _sleep(self, seconds: float) -> None:
        self._stopping.wait(timeout=seconds)


def relay_lag(
    session_factory: sessionmaker[Session], spec: OutboxSpec = TICK_OUTBOX
) -> Lag:
    """How far behind the relay is, and whether it is behind or blocked.

    Growing `rows` means behind; growing `oldest_age_seconds` means stopped;
    `held_back_seconds` (xid cursor) means an open writing transaction is
    holding the relay back, which is not a relay problem."""
    with session_factory() as session:
        cursor = cursor_for(spec)
        return cursor.lag(session, spec, cursor.read(session, spec))
