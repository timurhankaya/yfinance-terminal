"""The writer thread: ticks from the queue into PostgreSQL.

Runs in a worker thread, never on the event loop. The supervisor produces
into a bounded queue; this drains it in batches and writes them.

Two decisions here were measured rather than argued (see
docs/measurements/websocket.md):

**COPY, not INSERT.** The full write path manages 6,219 ticks/s with
`INSERT ... ON CONFLICT` and 22,291 with `COPY` into a staging table
followed by `INSERT ... SELECT`. At 10,000 symbols producing roughly a
message a second each, the first number does not meet the target and the
second does -- so COPY is a precondition, not an optimisation.

COPY also sidesteps a limit the INSERT path has to work around: the wire
protocol allows 65,535 bind parameters per statement, and at 36 columns
`live_ticks` would hit that ceiling at 1,820 rows (docs/measurements/database.md).
A COPY body binds nothing, so batch size here is a latency choice rather
than a protocol one.

**Small batches.** Going from 500 rows to 5,000 buys 6%. The batch stays
at 500, which keeps latency down and narrows the window a crash can lose.

The verification read the rest of the codebase performs is kept: it costs
2% here, so "every write is verified" survives into the live path without
a meaningful bill.
"""

from __future__ import annotations

import io
import json
import queue
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, NamedTuple

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from yfin.core import metrics
from yfin.core.logging_setup import get_logger
from yfin.models.stream import LiveQuote, LiveTick
from yfin.storage.contracts import TableWrite, WriteStats, apply_write
from yfin.storage.copy import copy_body, copy_value, jsonable
from yfin.storage.persistence import PostgresRowWriter
from yfin.stream.publish import TickPublisher
from yfin.stream.rejects import REJECT_UNKNOWN_SYMBOL, Reject
from yfin.stream.repository import StreamRepository
from yfin.stream.supervisor import StreamSupervisor

log = get_logger(__name__)

#: Column order for the COPY body. Taken from the model so it cannot drift
#: from the schema; `protocol.py` produces exactly these keys and a test
#: holds the two together.
TICK_COLUMNS: Final[tuple[str, ...]] = tuple(LiveTick.__table__.c.keys())

#: live_quotes carries the same measurement plus `updated_at`, and is
#: written by upsert rather than COPY: one row per symbol, guarded.
QUOTE_COLUMNS: Final[tuple[str, ...]] = tuple(LiveQuote.__table__.c.keys())


@dataclass
class WriterConfig:
    batch_size: int = 500
    batch_interval_ms: int = 250
    quotes_every_n_batches: int = 4
    reject_sample_per_hour: int = 100
    symbol_cache_seconds: float = 60.0
    #: With Kafka off the outbox is never written, so the second write
    #: path costs exactly nothing. Only whoever uses it pays for it.
    kafka_enabled: bool = False
    #: The browser fan-out (stream/publish.py). Off means no client is
    #: built and no socket is opened; the URL is env-only because it
    #: carries a credential.
    publish_enabled: bool = False
    publish_redis_url: str = ""


class TickWrite(NamedTuple):
    """What one `live_ticks` write produced.

    `accepted` is here rather than staying local because the browser
    fan-out publishes exactly these rows, after the commit. It holds the
    rows that passed the foreign-key filter, INCLUDING the duplicates
    `ON CONFLICT DO NOTHING` dropped -- the write path cannot tell those
    apart without a second read, so the same `(symbol, ts_utc)` can go
    out twice and the page drops the repeat.
    """

    written: int
    unknown: list[Reject]
    accepted: list[dict[str, Any]]


class SymbolFilter:
    """Keeps unknown symbols out of the batch.

    `live_ticks.symbol` carries a foreign key, so one symbol that is not
    in `symbols` aborts the entire statement -- 500 good ticks lost to one
    stray ticker. Filtering first is what makes the batch survivable.

    The cache is miss-tolerant: a symbol that is not in it triggers a real
    lookup rather than a rejection. An earlier design rejected on a cache
    miss, which would have discarded every tick of a newly added symbol
    for a whole TTL window -- and the reject sampling could have thrown
    away the evidence too.

    The `symbols` SELECTs below are deliberately NOT in `repository.py`,
    against the package rule. They ask whether a row EXISTS, which is the
    foreign key's question; `repository.load_scope` asks whether a symbol
    is ELIGIBLE (`is_active` plus the scope join). An inactive symbol
    still satisfies the FK, so reusing the repository query here would
    reject ticks the database would have accepted. Same table, different
    question. The cache also has to outlive a batch, so it owns its own
    session factory rather than borrowing the batch session.
    """

    def __init__(self, session_factory: sessionmaker[Session], ttl_seconds: float) -> None:
        self._session_factory = session_factory
        self._ttl = timedelta(seconds=ttl_seconds)
        self._known: set[str] = set()
        self._refreshed_at: datetime | None = None

    def known(self, candidates: set[str]) -> set[str]:
        if not candidates:
            return set()
        hits = candidates & self._known
        misses = candidates - self._known
        # Counted per SYMBOL, not per call: a batch of 500 ticks holding one
        # unknown symbol is 499 hits and one miss, and the ratio of calls
        # would report that as a 100 % miss.
        metrics.inc("yfin_cache_ops_total", len(hits), cache="symbol_filter", result="hit")
        if misses:
            metrics.inc(
                "yfin_cache_ops_total", len(misses), cache="symbol_filter", result="miss"
            )
        if misses and self._is_stale():
            self._refresh()
            hits = candidates & self._known
            misses = candidates - self._known
        if misses:
            # Still unknown after a refresh, or the cache is fresh but has
            # never seen these: ask the database rather than assume.
            hits |= self._lookup(misses)
        return hits

    def _is_stale(self) -> bool:
        return (
            self._refreshed_at is None
            or datetime.now(UTC) - self._refreshed_at > self._ttl
        )

    def _refresh(self) -> None:
        with self._session_factory() as session:
            rows = session.execute(text("SELECT symbol FROM symbols")).scalars().all()
        self._known = set(rows)
        self._refreshed_at = datetime.now(UTC)

    def _lookup(self, candidates: set[str]) -> set[str]:
        with self._session_factory() as session:
            rows = (
                session.execute(
                    text("SELECT symbol FROM symbols WHERE symbol = ANY(:s)"),
                    {"s": sorted(candidates)},
                )
                .scalars()
                .all()
            )
        found = set(rows)
        self._known |= found
        return found


class RejectSampler:
    """Caps how many reject rows one (symbol, reason) pair can write.

    A single broken feed would otherwise fill the table. Counts are never
    sampled -- the exact totals go on `stream_sessions` -- so this trims
    repetition, not information.
    """

    def __init__(self, per_hour: int) -> None:
        self._per_hour = per_hour
        self._seen: dict[tuple[str | None, str], tuple[datetime, int]] = {}

    def allow(self, reject: Reject) -> bool:
        key = (reject.symbol, reject.reason)
        now = datetime.now(UTC)
        window_start, count = self._seen.get(key, (now, 0))
        if now - window_start > timedelta(hours=1):
            window_start, count = now, 0
        if count >= self._per_hour:
            self._seen[key] = (window_start, count)
            return False
        self._seen[key] = (window_start, count + 1)
        return True


class StreamWriter:
    """Drains the supervisor's queue into the database."""

    def __init__(
        self,
        supervisor: StreamSupervisor,
        repository: StreamRepository,
        session_factory: sessionmaker[Session],
        *,
        config: WriterConfig | None = None,
        session_id: int | None = None,
    ) -> None:
        self._supervisor = supervisor
        self._repository = repository
        self._session_factory = session_factory
        self._config = config or WriterConfig()
        self._session_id = session_id
        self._filter = SymbolFilter(session_factory, self._config.symbol_cache_seconds)
        self._sampler = RejectSampler(self._config.reject_sample_per_hour)
        self._publisher = TickPublisher(
            enabled=self._config.publish_enabled, url=self._config.publish_redis_url
        )
        self._stopping = threading.Event()
        self._batches = 0
        self.rows_written = 0
        self.failed: BaseException | None = None

    # --- public API --------------------------------------------------------

    def run(self) -> None:
        """Writes until stopped, then drains what is left.

        A failure here is fatal to the process by design. If this thread
        dies the queue fills and the supervisor drops every tick while
        still looking healthy -- a process that is up and collecting
        nothing. `failed` is what the runner checks to decide that.
        """
        try:
            while not self._stopping.is_set():
                self._cycle()
            # Nothing in the queue may be lost to a clean stop.
            while self._drain_once():
                pass
        except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised by the runner
            self.failed = exc
            log.error("stream writer died", error=f"{type(exc).__name__}: {exc}")
        finally:
            # The one socket this thread owns outside the session factory.
            self._publisher.close()

    def stop(self) -> None:
        self._stopping.set()

    def set_session(self, session_id: int) -> None:
        self._session_id = session_id

    # --- batching ----------------------------------------------------------

    def _cycle(self) -> None:
        rows = self._collect()
        rejects = self._collect_rejects()
        # Health first, and on every cycle including the empty ones: a
        # connection that has gone quiet is exactly when its row matters,
        # and `_collect` returns on its own timeout when no tick arrives.
        self._flush_health()
        if not rows and not rejects:
            self._flush_counters()
            return
        self._write(rows, rejects)

    def _drain_once(self) -> bool:
        rows = self._collect(block=False)
        rejects = self._collect_rejects()
        if not rows and not rejects:
            return False
        self._write(rows, rejects)
        return True

    def _collect(self, *, block: bool = True) -> list[dict[str, Any]]:
        """Fills a batch, bounded by size and by time.

        Both bounds are needed. Without the size bound a busy open would
        build one enormous transaction; without the time bound the last
        ticks of a quiet market would sit in the queue for minutes.
        """
        rows: list[dict[str, Any]] = []
        deadline = time.monotonic() + self._config.batch_interval_ms / 1000
        while len(rows) < self._config.batch_size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                rows.append(
                    self._supervisor.queue.get(timeout=remaining) if block
                    else self._supervisor.queue.get_nowait()
                )
            except queue.Empty:
                break
        return rows

    def _collect_rejects(self) -> list[Reject]:
        rejects: list[Reject] = []
        while len(rejects) < self._config.batch_size:
            try:
                rejects.append(self._supervisor.rejects.get_nowait())
            except queue.Empty:
                break
        return rejects

    # --- writing -----------------------------------------------------------

    def _write(self, rows: Sequence[dict[str, Any]], rejects: Sequence[Reject]) -> None:
        """One batch, one transaction."""
        self._batches += 1
        # The number the batch interval has to stay under. Above it the
        # queue grows, and the queue overflowing is how ticks are lost --
        # so this histogram is the stream's single most important metric.
        #
        # The publish is INSIDE it, and that is deliberate: it runs on this
        # thread between two batches, so a Redis that is slow delays the
        # next `_collect` exactly the way a slow COPY does. Timing only the
        # transaction would leave the histogram healthy while the queue
        # grew, which is the one thing it exists to catch.
        with metrics.timed("yfin_stream_batch_seconds"):
            with self._session_factory() as session:
                written, unknown, accepted = self._write_ticks(session, rows)
                if self._config.kafka_enabled:
                    self._write_outbox(session, rows)
                # The FK filter's casualties go through the same path as
                # every other reject. They used to be counted nowhere and
                # logged at debug: a symbol dropped from `symbols` took its
                # whole tick stream with it and nothing said so.
                # `rows_written` still agreed with itself, which is exactly
                # why nobody would look.
                self._write_rejects(session, [*rejects, *unknown])
                if self._batches % self._config.quotes_every_n_batches == 0:
                    self._write_quotes(session)
                session.commit()
            # Committed, and only now. A tick published before the commit
            # is a price the page has and the archive does not -- and if
            # the transaction then rolls back, one it never will.
            self._publisher.publish(accepted)
        self.rows_written += written
        metrics.inc("yfin_stream_copy_rows_total", written, table="live_ticks")
        self._flush_counters(written=written)

    def _write_ticks(self, session: Session, rows: Sequence[dict[str, Any]]) -> TickWrite:
        """Rows written, the ticks the foreign key would not accept, and
        the ones the browser fan-out may publish once this commits."""
        if not rows:
            return TickWrite(0, [], [])

        # The FK filter runs before anything touches the table: one
        # unknown symbol would otherwise abort the statement and take the
        # whole batch with it.
        known = self._filter.known({row["symbol"] for row in rows})
        accepted = [row for row in rows if row["symbol"] in known]
        unknown = [
            Reject(
                reason=REJECT_UNKNOWN_SYMBOL,
                symbol=row["symbol"],
                detail="no row in symbols; the tick cannot reference one",
            )
            for row in rows
            if row["symbol"] not in known
        ]
        if unknown:
            log.warning(
                "ticks rejected for unknown symbols",
                count=len(unknown),
                symbols=sorted({reject.symbol for reject in unknown if reject.symbol}),
            )
        if not accepted:
            return TickWrite(0, unknown, [])

        columns = ", ".join(TICK_COLUMNS)
        # Created once per connection, emptied per batch. `ON COMMIT DROP`
        # plus a fresh CREATE would write to the system catalogue every
        # 250ms for the life of the process; this way the table is made
        # once and TRUNCATE (cheap on a temp table) clears it.
        #
        # The TRUNCATE is not redundant with ON COMMIT DELETE ROWS: that
        # fires on a real commit, and a batch that ends in a savepoint --
        # or a rolled-back transaction that left rows behind -- would
        # otherwise carry them into the next INSERT ... SELECT.
        session.execute(
            text(
                "CREATE TEMP TABLE IF NOT EXISTS stream_stage (LIKE live_ticks) "
                "ON COMMIT DELETE ROWS"
            )
        )
        session.execute(text("TRUNCATE stream_stage"))
        raw = session.connection().connection.driver_connection
        with raw.cursor().copy(  # type: ignore[union-attr]
            f"COPY stream_stage ({columns}) FROM STDIN"
        ) as copy:
            copy.write(copy_body(accepted, TICK_COLUMNS))
        session.execute(
            text(
                f"INSERT INTO live_ticks ({columns}) SELECT {columns} FROM stream_stage "
                f"ON CONFLICT (symbol, ts_utc, payload_hash) DO NOTHING"
            )
        )
        return TickWrite(self._verify(session, accepted), unknown, accepted)

    def _verify(self, session: Session, rows: Sequence[dict[str, Any]]) -> int:
        """Counts the keys that are actually present.

        The same guarantee the rest of the codebase gives: row counts come
        from reading the keys back, not from the driver's affected-row
        count, which `ON CONFLICT DO NOTHING` reports as zero anyway.

        The `ts_utc BETWEEN` clause is not redundant with the row
        constructor -- it is what makes this affordable. Measured against
        a 200-chunk hypertable:

            without the range clause:  200 chunks scanned, 21.5 ms
            with it:                     2 chunks scanned,  2.2 ms

        TimescaleDB cannot infer a time bound from a row-constructor `IN`,
        so without the clause every batch touches every chunk and the cost
        grows linearly with the age of the archive. On one day's data --
        how this was first measured -- the two are indistinguishable,
        which is exactly why it was missed.
        """
        timestamps = [row["ts_utc"] for row in rows]
        found = session.execute(
            text(
                "SELECT count(*) FROM live_ticks "
                " WHERE ts_utc >= :lo AND ts_utc <= :hi "
                "   AND (symbol, ts_utc, payload_hash) IN "
                "       (SELECT * FROM unnest(CAST(:symbols AS text[]), "
                "                             CAST(:timestamps AS timestamptz[]), "
                "                             CAST(:hashes AS text[])))"
            ),
            {
                "lo": min(timestamps),
                "hi": max(timestamps),
                "symbols": [row["symbol"] for row in rows],
                "timestamps": timestamps,
                "hashes": [row["payload_hash"] for row in rows],
            },
        ).scalar_one()
        return int(found)

    def _write_rejects(self, session: Session, rejects: Sequence[Reject]) -> None:
        # Counted BEFORE the sampler, and that is the point of counting them
        # here at all: the sampler caps how many rows one (symbol, reason)
        # pair may write, so `stream_rejects` deliberately under-reports a
        # storm. The metric is the number that does not.
        for reject in rejects:
            metrics.inc("yfin_stream_rejects_total", reason=reject.reason)
        sampled = [reject for reject in rejects if self._sampler.allow(reject)]
        if not sampled:
            return
        session.execute(
            text(
                "INSERT INTO stream_rejects (received_at, symbol, reason, detail, raw_base64) "
                "VALUES (:received_at, :symbol, :reason, :detail, :raw_base64)"
            ),
            [
                {
                    "received_at": datetime.now(UTC),
                    "symbol": reject.symbol,
                    "reason": reject.reason,
                    "detail": reject.detail,
                    "raw_base64": reject.raw_base64,
                }
                for reject in sampled
            ],
        )

    def _write_outbox(self, session: Session, rows: Sequence[dict[str, Any]]) -> None:
        """Queues the batch for Kafka, in the tick's own transaction.

        Atomic with the archive by construction: a row is in the outbox if
        and only if it is in live_ticks. That is the whole reason for an
        outbox rather than producing straight from the writer -- with a
        direct producer, a broker outage would leave ticks in the database
        that no consumer ever sees, and nothing would record the gap.

        Written with COPY like the ticks are. Measured: once live_ticks
        moved to COPY the bottleneck moved here, and an INSERT outbox held
        the whole path at 13.6k rows/s against 22.3k with both on COPY.

        `created_at` is this thread's clock, not the tick's received_at.
        The relay walks `id` but drops chunks by `created_at`, so the two
        have to agree -- a late tick carrying an old timestamp with a new
        id would land in a chunk the relay already considers finished.
        """
        if not rows:
            return
        now = datetime.now(UTC)
        exchanges = self._exchange_lookup(session, {row["symbol"] for row in rows})

        buffer = io.StringIO()
        for row in rows:
            payload = json.dumps(
                {k: jsonable(v) for k, v in row.items()},
                separators=(",", ":"),
                ensure_ascii=False,
            )
            buffer.write(
                "\t".join(
                    (
                        copy_value(now),
                        copy_value(row["symbol"]),
                        copy_value(exchanges.get(row["symbol"])),
                        copy_value(payload),
                    )
                )
                + "\n"
            )

        raw = session.connection().connection.driver_connection
        with raw.cursor().copy(  # type: ignore[union-attr]
            "COPY stream_outbox (created_at, symbol, exchange, payload) FROM STDIN"
        ) as copy:
            copy.write(buffer.getvalue())

    def _exchange_lookup(
        self, session: Session, symbols: set[str]
    ) -> dict[str, str | None]:
        """Exchange per symbol, from `symbols` -- never from the tick.

        The tick carries its own `exchange` field, raw and uppercased by
        nobody. Using it would let one exchange arrive as both `nms` and
        `NMS` and split a single Kafka topic in two, which quietly halves
        the per-symbol ordering guarantee.

        Not in `repository.py`, again against the package rule, and for a
        different reason than `SymbolFilter`'s: this runs on the BATCH
        session so the topic assignment is decided inside the same
        transaction as the outbox row it labels. Every `repository.py`
        method opens its own session, which would put this read outside
        that transaction.
        """
        if not symbols:
            return {}
        rows = session.execute(
            text("SELECT symbol, exchange FROM symbols WHERE symbol = ANY(:s)"),
            {"s": sorted(symbols)},
        ).all()
        return {row[0]: row[1] for row in rows}

    def _write_quotes(self, session: Session) -> None:
        """Upserts the latest quote per symbol, guarded on ts_utc.

        Read from the supervisor's last-value box, not from the batch.
        Two consequences, and both are the point:

          * the quote stays current even when the queue is overflowing and
            ticks are being dropped -- the box is not behind the queue;
          * a symbol that has not moved since the last flush is not
            rewritten. Measured, this upsert was 34% of the write path,
            and it is the one part of the batch worth skipping.

        Every N batches rather than every batch: live_quotes is a derived
        view of live_ticks, so a few hundred milliseconds of staleness
        costs nothing that matters.
        """
        rows = self._supervisor.latest.drain()
        if not rows:
            return

        known = self._filter.known({row["symbol"] for row in rows})
        accepted = [
            {**{name: row.get(name) for name in TICK_COLUMNS}, "updated_at": datetime.now(UTC)}
            for row in rows
            if row["symbol"] in known
        ]
        if not accepted:
            return

        write = TableWrite(
            table="live_quotes",
            rows=accepted,
            key_columns=("symbol",),
            update_columns=tuple(c for c in QUOTE_COLUMNS if c != "symbol"),
            # An older tick must update NOTHING. A per-column GREATEST
            # would mix two instants into a row that never existed.
            guard_column="ts_utc",
        )
        apply_write(PostgresRowWriter(session), write, WriteStats())

    def _flush_health(self) -> None:
        """Writes the health the event loop recorded in memory.

        Here rather than where it is emitted: this thread owns the
        database and the event loop must never wait on it. `heartbeat_at`
        is stamped by the repository at write time, which now also proves
        the writer thread is alive -- a row that stopped being refreshed
        is the honest signal either way.
        """
        if self._session_id is None:
            # Nothing to attach the rows to yet. They are left in the box,
            # which keeps only the newest per connection, so waiting costs
            # nothing and loses nothing.
            return
        for health in self._supervisor.health.drain():
            self._repository.record_health(
                self._session_id,
                connection_key=health.connection_key,
                state=health.state,
                subscribed_count=health.subscribed_count,
                connected_at=health.connected_at,
                last_message_at=health.last_message_at,
                last_canary_at=health.last_canary_at,
                reconnect_count=health.reconnect_count,
                last_error=health.last_error,
            )

    def _flush_counters(self, *, written: int = 0) -> None:
        """Pushes the supervisor's counters onto the session row.

        Additive, and only from here: the supervisor increments in the
        event loop and this thread owns the database, so the counters
        cross the boundary exactly once per batch.
        """
        if self._session_id is None:
            return
        counters = self._supervisor.drain_counters()
        # Drained here whether or not there is a row to write: the counters
        # cross the thread boundary exactly once per batch, and returning
        # early below must not be what decides whether they are published.
        if counters.messages:
            metrics.inc("yfin_stream_messages_total", counters.messages)
        if not (counters.messages or counters.dropped or counters.rejected or written):
            return
        self._repository.add_session_counters(
            self._session_id,
            messages=counters.messages,
            written=written,
            rejected=counters.rejected,
            dropped=counters.dropped,
        )
