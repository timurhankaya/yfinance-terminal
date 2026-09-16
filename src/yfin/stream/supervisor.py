"""Runs the connection set and feeds the writer.

Owns the event loop. The tick queue is bounded and drops under load; the
last-value box sits beside it, so `live_quotes` stays current meanwhile.
"""

from __future__ import annotations

import asyncio
import contextlib
import queue
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
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
        """Returns the accumulated counts and resets them; the writer adds them to the row."""
        snapshot = StreamCounters(self.messages, self.dropped, self.rejected)
        self.messages = self.dropped = self.rejected = 0
        return snapshot


class LatestBox:
    """The most recent tick per symbol, for `live_quotes`.

    Written from the event loop, read from the writer thread; the lock is
    never held around I/O. `drain` empties it so unchanged symbols skip.
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



class HealthBox:
    """The latest health row per connection, for `stream_connection_health`.

    Emitted from the event loop, which must not block on the database. A
    copy is stored: `ConnectionHealth` is mutable and may change mid-read.
    """

    def __init__(self) -> None:
        self._rows: dict[str, ConnectionHealth] = {}
        self._lock = threading.Lock()

    def put(self, health: ConnectionHealth) -> None:
        with self._lock:
            self._rows[health.connection_key] = replace(health)

    def drain(self) -> list[ConnectionHealth]:
        with self._lock:
            drained = list(self._rows.values())
            self._rows = {}
        return drained

    def __len__(self) -> int:
        with self._lock:
            return len(self._rows)


@dataclass
class SupervisorConfig:
    """The settings the supervisor reads.

    A plain dataclass, not `Settings`: the supervisor re-reads its
    configuration on every rescan, and `get_settings()` is a singleton.
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
        self.health = HealthBox()
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
            # Unsynced symbols all land on one connection.
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

        A connection whose membership did not change is left alone:
        every reconnect is a gap in the archive.
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
            # A partial unsubscribe plus re-subscribe re-triggers Yahoo's
            # truncation; a clean restart is quota-correct.
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

        Never blocks: the producer is the event loop, and blocking it
        leaves ping/pong unanswered, so Yahoo drops the connection.
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
        """Records health in memory; the writer thread puts it on disk.

        Called from the event loop, so nothing here may touch the database.
        """
        entry = self._running.get(health.connection_key)
        if entry is not None:
            entry.health = health
        self.health.put(health)

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

