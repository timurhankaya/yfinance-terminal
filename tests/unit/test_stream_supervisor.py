"""Supervisor: connection set, backpressure and the last-value box.

The repository is faked here so these stay unit tests; the SQL it stands
in for is covered against a real database in tests/repo.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from yfin.stream.connection import STATE_OPEN, ConnectionHealth
from yfin.stream.protocol import REJECT_DECODE_FAILED, DecodeResult, Reject
from yfin.stream.repository import ScopeEntry
from yfin.stream.supervisor import (
    LatestBox,
    StreamCounters,
    StreamSupervisor,
    SupervisorConfig,
)
from yfin.stream.topology import QuotaExceeded

TS = datetime(2026, 9, 7, 14, 30, tzinfo=UTC)


class FakeRepository:
    """Stands in for the SQL, records what the supervisor asked for."""

    def __init__(self, scope: list[ScopeEntry] | None = None) -> None:
        self.scope = scope or []
        self.sessions: list[tuple[int, int]] = []
        self.health: list[dict[str, Any]] = []
        self.forgotten: list[str] = []
        self.deleted_quotes: list[str] = []
        self.missing_exchange = 0

    def load_scope(self) -> list[ScopeEntry]:
        return list(self.scope)

    def count_symbols_missing_exchange(self) -> int:
        return self.missing_exchange

    def open_session(self, *, connection_count: int, symbol_count: int) -> int:
        self.sessions.append((connection_count, symbol_count))
        return 1

    def record_health(self, session_id: int, **fields: Any) -> None:
        self.health.append(fields)

    def forget_connections(self, keys: Any) -> None:
        self.forgotten.extend(keys)

    def delete_quotes(self, symbols: Any) -> int:
        self.deleted_quotes.extend(symbols)
        return len(list(symbols))


def _entry(symbol: str, exchange: str | None = "NMS", *, archive: bool = True) -> ScopeEntry:
    return ScopeEntry(symbol=symbol, exchange=exchange, archive=archive)


def _row(symbol: str = "AAPL", ts: datetime = TS) -> dict[str, Any]:
    return {"symbol": symbol, "ts_utc": ts, "price": 1}


def _supervisor(
    repository: FakeRepository, **overrides: Any
) -> StreamSupervisor:
    config = SupervisorConfig(**overrides)
    return StreamSupervisor(repository, config=config)  # type: ignore[arg-type]


# --- last-value box --------------------------------------------------------


def test_latest_box_keeps_one_row_per_symbol() -> None:
    box = LatestBox()
    box.put(_row("AAPL"))
    box.put(_row("AAPL", TS + timedelta(seconds=1)))
    box.put(_row("MSFT"))
    assert len(box) == 2


def test_latest_box_keeps_the_newest() -> None:
    """Out-of-order delivery must not roll the quote back.

    The database guard alone cannot help here: the writer collapses a
    batch to one row per symbol before the guard ever sees it.
    """
    box = LatestBox()
    box.put(_row("AAPL", TS))
    box.put(_row("AAPL", TS - timedelta(minutes=5)))
    assert box.drain()[0]["ts_utc"] == TS


def test_latest_box_drain_empties_it() -> None:
    """A symbol that has not ticked again does not need re-writing --
    the quotes upsert is the most expensive part of the batch."""
    box = LatestBox()
    box.put(_row("AAPL"))
    assert len(box.drain()) == 1
    assert box.drain() == []


# --- counters --------------------------------------------------------------


def test_counters_take_resets() -> None:
    """Additive flush: a running total would double-count every batch."""
    counters = StreamCounters(messages=5, dropped=2, rejected=1)
    taken = counters.take()
    assert (taken.messages, taken.dropped, taken.rejected) == (5, 2, 1)
    assert (counters.messages, counters.dropped, counters.rejected) == (0, 0, 0)


# --- routing ---------------------------------------------------------------


def test_archived_symbol_reaches_the_queue() -> None:
    repository = FakeRepository([_entry("AAPL")])
    supervisor = _supervisor(repository)
    supervisor._archived = {"AAPL"}
    supervisor._on_result(DecodeResult(row=_row("AAPL")))
    assert supervisor.queue.qsize() == 1
    assert len(supervisor.latest) == 1


def test_unarchived_symbol_is_quoted_but_not_archived() -> None:
    """`archive = false` is the volume dial: live price, no tick history."""
    repository = FakeRepository([_entry("AAPL", archive=False)])
    supervisor = _supervisor(repository)
    supervisor._archived = set()
    supervisor._on_result(DecodeResult(row=_row("AAPL")))
    assert supervisor.queue.qsize() == 0
    assert len(supervisor.latest) == 1


def test_rejects_are_counted_and_queued() -> None:
    supervisor = _supervisor(FakeRepository())
    supervisor._on_result(DecodeResult(rejects=[Reject(REJECT_DECODE_FAILED)]))
    assert supervisor.counters.rejected == 1
    assert supervisor.rejects.qsize() == 1


# --- backpressure ----------------------------------------------------------


def test_full_queue_drops_and_counts() -> None:
    """Blocking would stall the event loop, so ping/pong goes unanswered
    and Yahoo drops the connection -- losing every symbol instead of one
    tick."""
    supervisor = _supervisor(FakeRepository(), queue_maxsize=2)
    supervisor._archived = {"AAPL"}
    for i in range(5):
        supervisor._on_result(DecodeResult(row=_row("AAPL", TS + timedelta(seconds=i))))
    assert supervisor.queue.qsize() == 2
    assert supervisor.counters.dropped == 3


def test_quotes_survive_a_full_queue() -> None:
    """The reason the box is not downstream of the queue.

    An earlier design claimed quotes were preserved on overflow while the
    queue was the only channel to the writer -- which made the claim
    impossible.
    """
    supervisor = _supervisor(FakeRepository(), queue_maxsize=1)
    supervisor._archived = {"AAPL"}
    for i in range(5):
        supervisor._on_result(DecodeResult(row=_row("AAPL", TS + timedelta(seconds=i))))
    assert supervisor.counters.dropped == 4
    latest = supervisor.latest.drain()
    assert latest[0]["ts_utc"] == TS + timedelta(seconds=4)


def test_full_reject_queue_does_not_raise() -> None:
    supervisor = _supervisor(FakeRepository(), queue_maxsize=1)
    for _ in range(5):
        supervisor._on_result(DecodeResult(rejects=[Reject(REJECT_DECODE_FAILED)]))
    assert supervisor.counters.rejected == 5


# --- health ----------------------------------------------------------------


def test_health_is_recorded_once_a_session_exists() -> None:
    repository = FakeRepository()
    supervisor = _supervisor(repository)
    supervisor._session_id = 1
    supervisor._on_health(
        ConnectionHealth(connection_key="NMS", state=STATE_OPEN, subscribed_count=3)
    )
    assert repository.health[0]["connection_key"] == "NMS"
    assert repository.health[0]["state"] == STATE_OPEN


def test_health_before_a_session_is_ignored() -> None:
    """Connections emit state while the session row is still being opened."""
    repository = FakeRepository()
    supervisor = _supervisor(repository)
    supervisor._on_health(ConnectionHealth(connection_key="NMS"))
    assert repository.health == []


def test_stale_threshold_is_two_rescans() -> None:
    """One missed heartbeat can be a slow tick; two means nothing writes."""
    supervisor = _supervisor(FakeRepository(), rescan_seconds=30.0)
    assert supervisor.stale_after_seconds() == 60.0


# --- planning --------------------------------------------------------------


def test_plan_respects_the_quota_with_the_canary() -> None:
    scope = [_entry(f"S{i:04d}") for i in range(200)]
    supervisor = _supervisor(FakeRepository(scope), max_symbols_per_connection=95)
    plans = supervisor._plan(scope)
    for plan in plans:
        assert len(plan.subscription(("BTC-USD",))) <= 100


def test_plan_over_the_ceiling_raises() -> None:
    scope = [_entry(f"S{i:04d}") for i in range(1000)]
    supervisor = _supervisor(FakeRepository(scope), max_connections=2)
    with pytest.raises(QuotaExceeded):
        supervisor._plan(scope)


# --- lifecycle -------------------------------------------------------------


async def test_run_opens_a_session_and_starts_connections() -> None:
    scope = [_entry("AAPL"), _entry("XU100", "IST")]
    repository = FakeRepository(scope)
    supervisor = _supervisor(repository, rescan_seconds=10.0)

    async def never_connect(url: str) -> Any:
        await asyncio.sleep(3600)

    supervisor._connector = never_connect  # type: ignore[assignment]
    task = asyncio.create_task(supervisor.run())
    await asyncio.sleep(0.05)
    keys = supervisor.connection_keys
    supervisor.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert keys == ["IST", "NMS"]
    assert repository.sessions == [(2, 2)]


async def test_stop_closes_every_connection() -> None:
    repository = FakeRepository([_entry("AAPL")])
    supervisor = _supervisor(repository, rescan_seconds=0.05)

    async def never_connect(url: str) -> Any:
        await asyncio.sleep(3600)

    supervisor._connector = never_connect  # type: ignore[assignment]
    task = asyncio.create_task(supervisor.run())
    await asyncio.sleep(0.05)
    supervisor.stop()
    async with asyncio.timeout(2.0):
        await task
    assert supervisor.connection_keys == []


async def test_rescan_opens_a_connection_for_a_new_exchange() -> None:
    repository = FakeRepository([_entry("AAPL")])
    supervisor = _supervisor(repository, rescan_seconds=0.02)

    async def never_connect(url: str) -> Any:
        await asyncio.sleep(3600)

    supervisor._connector = never_connect  # type: ignore[assignment]
    task = asyncio.create_task(supervisor.run())
    await asyncio.sleep(0.05)
    repository.scope = [_entry("AAPL"), _entry("XU100", "IST")]
    await asyncio.sleep(0.1)
    keys = supervisor.connection_keys
    supervisor.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert keys == ["IST", "NMS"]


async def test_rescan_drops_quotes_for_departed_symbols() -> None:
    """live_quotes does not trim itself; left alone it only grows."""
    repository = FakeRepository([_entry("AAPL"), _entry("MSFT")])
    supervisor = _supervisor(repository, rescan_seconds=0.02)

    async def never_connect(url: str) -> Any:
        await asyncio.sleep(3600)

    supervisor._connector = never_connect  # type: ignore[assignment]
    task = asyncio.create_task(supervisor.run())
    await asyncio.sleep(0.05)
    repository.scope = [_entry("AAPL")]
    await asyncio.sleep(0.1)
    supervisor.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert repository.deleted_quotes == ["MSFT"]


async def test_a_bad_plan_does_not_kill_the_run() -> None:
    """A scope edit that overflows the ceiling must not stop collection
    on the connections that are already working."""
    repository = FakeRepository([_entry("AAPL")])
    supervisor = _supervisor(repository, rescan_seconds=0.02, max_connections=1)

    async def never_connect(url: str) -> Any:
        await asyncio.sleep(3600)

    supervisor._connector = never_connect  # type: ignore[assignment]
    task = asyncio.create_task(supervisor.run())
    await asyncio.sleep(0.05)
    repository.scope = [_entry("AAPL"), _entry("XU100", "IST"), _entry("X", "ASE")]
    await asyncio.sleep(0.1)
    alive = supervisor.connection_keys
    supervisor.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert alive == ["NMS"]
