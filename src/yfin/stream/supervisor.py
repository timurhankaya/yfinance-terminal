"""Runs the connection set and feeds the writer.

The supervisor owns the event loop. It holds no SQL of its own (that is
`repository.py`) and decodes nothing (that is `protocol.py`); what it
owns is the part that has to be right for the process to keep collecting
data: how many connections exist, what each one is subscribed to, and
what happens when the writer falls behind.

Two structures matter more than the rest.

**The queue** carries ticks to the writer thread and is bounded. When it
fills, ticks are dropped and counted -- see `_offer` for why blocking and
pausing are both worse.

**The last-value box** is separate from the queue on purpose. `live_quotes`
is fed from here, not from the writer, so the "what is the price now"
answer stays current even while the archive is shedding load. An earlier
design had it downstream of the queue and claimed quotes survived an
overflow; they could not have, because the queue was the only channel.
"""

from __future__ import annotations

import asyncio
import contextlib
import queue
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from yfin.core.logging_setup import get_logger
from yfin.stream.connection import ConnectionHealth, Connector, StreamConnection
from yfin.stream.rejects import DecodeResult, Reject
from yfin.stream.repository import ScopeEntry, StreamRepository
from yfin.stream.topology import ConnectionPlan, plan_connections, plan_diff

log = get_logger(__name__)


@dataclass
class StreamCounters:
    """What the writer flushes onto `stream_sessions`."""

    messages: int = 0
    dropped: int = 0
    rejected: int = 0

    def take(self) -> StreamCounters:
        """Returns the accumulated counts and resets them.

        Read-and-reset rather than a running total: the writer adds these
        to the session row, so holding a cumulative value here would
        double-count on every flush.
        """
        snapshot = StreamCounters(self.messages, self.dropped, self.rejected)
        self.messages = self.dropped = self.rejected = 0
        return snapshot


class LatestBox:
    """The most recent tick per symbol, for `live_quotes`.

    Written from the event loop and read from the writer thread, so it
    takes a lock. The lock is held only around a dict assignment and a
    dict swap -- never around I/O.

    `drain` empties the box: a symbol that has not ticked since the last
    flush does not need writing again, and re-writing it would burn the
    most expensive part of the batch (measured: the quotes upsert alone
    cost 34% of the write path).
    """

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def put(self, row: dict[str, Any]) -> None:
        with self._lock:
            current = self._rows.get(row["symbol"])
            # Keep the newest. Out-of-order delivery must not roll the
            # quote back, and the database guard alone would not help --
            # the writer collapses a batch to one row per symbol before
            # the guard ever sees it.
            if current is None or row["ts_utc"] >= current["ts_utc"]:
                self._rows[row["symbol"]] = row

    def drain(self) -> list[dict[str, Any]]:
        with self._lock:
            drained = list(self._rows.values())
            self._rows = {}
        return drained

    def __len__(self) -> int:
        with self._lock:
            return len(self._rows)


@dataclass
class SupervisorConfig:
    """The settings the supervisor actually reads.

    A plain dataclass rather than the pipeline `Settings` object: the
    supervisor re-reads its configuration on every rescan, and
    `get_settings()` is a process-lifetime singleton that never re-reads
    the settings table. Passing the resolved values in keeps that
    distinction visible instead of hiding a stale singleton behind a
    property.
    """

    max_connections: int = 256
    max_symbols_per_connection: int = 95
    queue_maxsize: int = 10_000
    idle_timeout_seconds: float = 300.0
    reconnect_max_seconds: float = 60.0
    rescan_seconds: float = 60.0
    canary_symbols: tuple[str, ...] = ("BTC-USD",)


@dataclass
class _Running:
    plan: ConnectionPlan
    connection: StreamConnection
    task: asyncio.Task[None]
    health: ConnectionHealth = field(default_factory=lambda: ConnectionHealth(""))


class StreamSupervisor:
    """Keeps the connection set in line with the scope table."""

    def __init__(
        self,
        repository: StreamRepository,
        *,
        config: SupervisorConfig,
        config_loader: Callable[[], SupervisorConfig] | None = None,
        url: str | None = None,
        connector: Connector | None = None,
    ) -> None:
        self._repository = repository
        self._config = config
        self._config_loader = config_loader
        self._url = url
        self._connector = connector

        self.queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=config.queue_maxsize)
        self.latest = LatestBox()
        self.counters = StreamCounters()
        self.rejects: queue.Queue[Reject] = queue.Queue(maxsize=config.queue_maxsize)

        self._running: dict[str, _Running] = {}
        self._archived: set[str] = set()
        self._stopping = asyncio.Event()
        self._session_id: int | None = None

    # --- public API --------------------------------------------------------

    @property
    def connection_keys(self) -> list[str]:
        return sorted(self._running)

    async def run(self) -> None:
        """Builds the connection set and keeps it aligned until stopped."""
        scope = self._repository.load_scope()
        plans = self._plan(scope)
        self._session_id = self._repository.open_session(
            connection_count=len(plans), symbol_count=len(scope)
        )
        self._archived = {entry.symbol for entry in scope if entry.archive}

        missing = self._repository.count_symbols_missing_exchange()
        if missing:
            # Same warning the sync CLI gives: `yfin symbols add` leaves
            # exchange NULL until the first sync, and those symbols all
            # land on one connection without anyone being told.
            log.warning("scoped symbols have no exchange yet", count=missing)

        try:
            await self._apply(plans)
            while not self._stopping.is_set():
                await self._sleep_until_rescan()
                if self._stopping.is_set():
                    break
                await self._rescan()
        finally:
            await self._close_all()

    def stop(self) -> None:
        self._stopping.set()

    def drain_counters(self) -> StreamCounters:
        return self.counters.take()

    # --- planning ----------------------------------------------------------

    def _plan(self, scope: Sequence[ScopeEntry]) -> list[ConnectionPlan]:
        return plan_connections(
            [(entry.symbol, entry.exchange) for entry in scope],
            max_symbols_per_connection=self._config.max_symbols_per_connection,
            max_connections=self._config.max_connections,
            canary_count=len(self._config.canary_symbols),
        )

    async def _apply(self, plans: Sequence[ConnectionPlan]) -> None:
        for plan in plans:
            self._start(plan)

    def _start(self, plan: ConnectionPlan) -> None:
        connection = StreamConnection(
            plan,
            on_result=self._on_result,
            on_health=self._on_health,
            canary=self._config.canary_symbols,
            idle_timeout=self._config.idle_timeout_seconds,
            reconnect_max_seconds=self._config.reconnect_max_seconds,
            connector=self._connector,
            **({"url": self._url} if self._url else {}),
        )
        task = asyncio.create_task(connection.run(), name=f"stream-{plan.key}")
        self._running[plan.key] = _Running(plan=plan, connection=connection, task=task)

    async def _rescan(self) -> None:
        """Re-reads scope and settings, then adjusts the connection set.

        Surgical by design: a connection whose membership did not change
        is left alone. Every reconnect is a gap in the archive, and
        re-sending a subscription re-applies Yahoo's 100-symbol
        truncation.
        """
        if self._config_loader is not None:
            self._config = self._config_loader()

        scope = self._repository.load_scope()
        try:
            desired = self._plan(scope)
        except Exception as exc:  # noqa: BLE001 - a bad plan must not kill the run
            log.error("rescan planning failed", error=str(exc))
            return

        current = [entry.plan for entry in self._running.values()]
        to_open, to_close, changes = plan_diff(current, desired)

        for key in to_close:
            await self._close(key)
        self._repository.forget_connections(to_close)

        for plan in to_open:
            self._start(plan)

        for key, (added, removed) in changes.items():
            entry = self._running[key]
            entry.plan = next(p for p in desired if p.key == key)
            log.info("subscription changed", connection=key,
                     added=len(added), removed=len(removed))
            # Restarting the connection is the honest way to change a
            # subscription set: Yahoo's `unsubscribe` frees slots, but the
            # combination of a partial unsubscribe and a re-subscribe is
            # what re-triggers truncation. A single clean re-subscribe is
            # both simpler and quota-correct.
            await self._close(key)
            self._start(entry.plan)

        gone = self._departed_symbols(scope)
        if gone:
            self._repository.delete_quotes(sorted(gone))
        self._archived = {entry.symbol for entry in scope if entry.archive}

    def _departed_symbols(self, scope: Sequence[ScopeEntry]) -> set[str]:
        previous = set(self._archived)
        return previous - {entry.symbol for entry in scope}

    # --- callbacks from connections ---------------------------------------

    def _on_result(self, result: DecodeResult) -> None:
        self.counters.messages += 1
        for reject in result.rejects:
            self.counters.rejected += 1
            self._offer_reject(reject)
        row = result.row
        if row is None:
            return

        # Always current, even under load: the box is not behind the
        # queue, so a full queue costs tick history but never the latest
        # price.
        self.latest.put(row)

        if row["symbol"] not in self._archived:
            # archive=false: quoted but not kept.
            return
        self._offer(row)

    def _offer(self, row: dict[str, Any]) -> None:
        """Hands a tick to the writer, or drops it and counts.

        Three options when the queue is full, and the third is chosen:

          1. Block. `pipeline/runner.py` does this and is right to -- its
             producer is our own worker. Here the producer is the event
             loop: blocking it means ping/pong goes unanswered, Yahoo
             drops the connection, and EVERY symbol stops.
          2. Stop reading (TCP backpressure). Same outcome, slower.
          3. Drop and count. The tick is lost; the fact that it was lost
             is not.

        The third is not a compromise on completeness -- the alternative
        is crashing and losing all of them. What must never happen is
        losing them silently.
        """
        try:
            self.queue.put_nowait(row)
        except queue.Full:
            self.counters.dropped += 1

    def _offer_reject(self, reject: Reject) -> None:
        # Rejects are already sampled downstream; dropping one under
        # pressure beats growing an unbounded buffer at the exact moment
        # the process is struggling.
        with contextlib.suppress(queue.Full):
            self.rejects.put_nowait(reject)

    def _on_health(self, health: ConnectionHealth) -> None:
        entry = self._running.get(health.connection_key)
        if entry is not None:
            entry.health = health
        if self._session_id is None:
            return
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

    # --- lifecycle ---------------------------------------------------------

    async def _sleep_until_rescan(self) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                self._stopping.wait(), timeout=self._config.rescan_seconds
            )

    async def _close(self, key: str) -> None:
        entry = self._running.pop(key, None)
        if entry is None:
            return
        entry.connection.stop()
        entry.task.cancel()
        await asyncio.gather(entry.task, return_exceptions=True)

    async def _close_all(self) -> None:
        for key in list(self._running):
            await self._close(key)

    def stale_after_seconds(self) -> float:
        """When a health row stops counting as live.

        Two rescan intervals: one missed heartbeat can be a slow tick, two
        means nothing is writing them.
        """
        return self._config.rescan_seconds * 2

