"""Outbox -> Kafka, and the chunk cleanup behind it.

Runs as its own process, separate from whatever fills the queue, so a
broker outage cannot slow down or stop the work. An outbox row is written
inside the same transaction as the data it announces, so a row is in the
queue if and only if it is in the database.

The guarantee is **at-least-once**. If the process dies between publishing
and committing the offset, the same messages are published again. Exactly
once would mean a two-phase commit between Kafka and PostgreSQL, which buys
nothing a consumer-side dedupe does not already provide -- ticks dedupe on
`live_ticks`' primary key, which travels in the payload.

Which table, which lock, which column picks the topic and which becomes the
key are all read from an `OutboxSpec` rather than branched on here; see
`spec.py` for why.

Ordering for the tick outbox rests on two invariants its writer maintains
(see `stream/writer.py`): a single writer thread, so `id` follows commit
order, and a `created_at` generated at write time, so it agrees with `id`.
The first makes `id > offset` correct; the second makes dropping old chunks
safe. An outbox written by concurrent transactions cannot claim either, and
walks transaction ids instead.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from yfin.core.logging_setup import get_logger
from yfin.outbox.cursor import Lag, Position, cursor_for
from yfin.outbox.kafka import (
    KafkaUnavailable,
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

        Checked rather than left to `auto.create.topics.enable`: an
        implicitly created topic takes the broker's default partition
        count, and the wrong count silently costs the per-key ordering the
        whole key scheme exists for.
        """
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

        The offset moves only after every delivery is acknowledged. That
        ordering is the whole correctness argument: advancing first would
        step past messages a broker outage swallowed, and the outbox is the
        only place they exist.
        """
        with self._session_factory() as session:
            at = self._cursor.read(session, self._spec)
            messages = self._cursor.batch(
                session, self._spec, at, self._config.batch_size
            )
        if not messages:
            return 0

        tracker = publish(producer, messages, spec=self._spec)
        if not tracker.ok:
            # Deliberately not advancing: the same rows are retried on the
            # next pass. At-least-once is the contract, so a duplicate is
            # acceptable and a gap is not.
            self.stats.last_error = tracker.failed[0]
            self.stats.failures.extend(tracker.failed[:5])
            log.error(
                "kafka delivery failed; offset not advanced",
                outbox=self._spec.table,
                delivered=tracker.delivered,
                failed=len(tracker.failed),
            )
            return 0

        last = messages[-1]
        with self._session_factory() as session:
            self._cursor.advance(
                session, self._spec, Position(xid=last.xid or 0, id=last.id)
            )
        self.stats.published += len(messages)
        self.stats.passes += 1
        return len(messages)

    # --- cleanup -----------------------------------------------------------

    def _maybe_cleanup(self) -> None:
        now = time.monotonic()
        if now - self._last_cleanup < self._config.cleanup_every_seconds:
            return
        self._last_cleanup = now
        self.stats.chunks_dropped += self.drop_published_chunks()

    def drop_published_chunks(self) -> int:
        """Drops outbox chunks whose rows are all published.

        `drop_chunks` rather than DELETE, and that is what makes the outbox
        affordable at all: at ~1.5 billion rows a year, a DELETE plus
        autovacuum could not keep up with the queue.

        The cutoff is the `created_at` of the oldest UNPUBLISHED row, so a
        chunk is only dropped once nothing in it is still waiting. This is
        safe because `created_at` is generated by the writer in `id`
        order -- a late tick can never appear with a low `created_at` and a
        high `id`.
        """
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

    What `yfin stream status` and `yfin changes status` report. A growing
    `rows` means the relay is behind; a growing `oldest_age_seconds` means
    it is stopped. On the `xid` cursor a third number tells those apart from
    a third case: the relay cannot pass an open writing transaction, so a
    backlog with `held_back_seconds` set is not a relay problem at all.
    """
    with session_factory() as session:
        cursor = cursor_for(spec)
        return cursor.lag(session, spec, cursor.read(session, spec))


__all__ = [
    "KafkaUnavailable",
    "OutboxRelay",
    "RelayConfig",
    "Lag",
    "RelayStats",
    "relay_lag",
]
