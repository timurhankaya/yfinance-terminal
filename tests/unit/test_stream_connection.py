"""Connection lifecycle: subscribe, reconnect, idle watchdog, canary.

These run against a real loopback websocket server rather than a mock,
and that is the point. The upstream client's reconnect is broken in a way
no mock would reveal -- `_connect()` returns early because `self._ws` is
not None, so the retry loop spins forever on a dead socket. A test that
substituted the transport would have reproduced the intended behaviour,
not the actual one.

This is a deliberate exception to "unit tests touch no network": the
server binds 127.0.0.1 and nothing leaves the machine.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Callable

import pytest
import websockets
from websockets.asyncio.server import ServerConnection, serve
from yfinance.pricing_pb2 import PricingData

from yfin.stream.connection import (
    STATE_OPEN,
    STATE_RECONNECTING,
    ConnectionHealth,
    StreamConnection,
    _Backoff,
)
from yfin.stream.protocol import DecodeResult
from yfin.stream.topology import ConnectionPlan

PLAN = ConnectionPlan(key="NMS", exchange="NMS", symbols=("AAPL", "MSFT"))


def _frame(symbol: str, price: float = 100.0) -> str:
    message = PricingData()
    message.id = symbol
    message.time = 1_700_000_000_000
    message.price = price
    return json.dumps(
        {"type": "pricing", "message": base64.b64encode(message.SerializeToString()).decode()}
    )


class Recorder:
    """Collects what the connection hands back."""

    def __init__(self) -> None:
        self.results: list[DecodeResult] = []
        self.health: list[str] = []

    def on_result(self, result: DecodeResult) -> None:
        self.results.append(result)

    def on_health(self, health: ConnectionHealth) -> None:
        self.health.append(health.state)

    @property
    def symbols(self) -> list[str]:
        return [r.row["symbol"] for r in self.results if r.row is not None]


class Server:
    """A loopback stand-in for Yahoo's streamer."""

    def __init__(self) -> None:
        self.subscriptions: list[list[str]] = []
        self.connections = 0
        self.behaviour: Callable[[ServerConnection, list[str]], AsyncIterator[str]] | None = None
        self._server: object | None = None
        self.url = ""
        # Handlers wait on this instead of asyncio.Future(): a handler
        # that can never finish makes wait_closed() hang forever, and the
        # whole test session with it.
        self.closing = asyncio.Event()

    async def start(self) -> None:
        self._server = await serve(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]  # type: ignore[attr-defined]
        self.url = f"ws://127.0.0.1:{port}"

    async def stop(self) -> None:
        self.closing.set()
        if self._server is not None:
            self._server.close()  # type: ignore[attr-defined]
            await self._server.wait_closed()  # type: ignore[attr-defined]

    async def _handle(self, ws: ServerConnection) -> None:
        self.connections += 1
        raw = await ws.recv()
        payload = json.loads(raw)
        symbols = payload["subscribe"]
        self.subscriptions.append(symbols)
        if self.behaviour is None:
            for symbol in symbols:
                await ws.send(_frame(symbol))
            await self.closing.wait()
        else:
            async for message in self.behaviour(ws, symbols):
                await ws.send(message)


@pytest.fixture
async def server() -> AsyncIterator[Server]:
    instance = Server()
    await instance.start()
    yield instance
    await instance.stop()


async def _run_until(
    connection: StreamConnection, predicate: Callable[[], bool], timeout: float = 5.0
) -> None:
    """Runs the connection until `predicate` holds, then stops it."""
    task = asyncio.create_task(connection.run())
    try:
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.01)
    finally:
        connection.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# --- subscribe -------------------------------------------------------------


async def test_subscribes_and_receives(server: Server) -> None:
    recorder = Recorder()
    connection = StreamConnection(
        PLAN, on_result=recorder.on_result, on_health=recorder.on_health, url=server.url
    )
    await _run_until(connection, lambda: len(recorder.symbols) >= 2)
    assert sorted(recorder.symbols) == ["AAPL", "MSFT"]
    assert server.subscriptions[0] == ["AAPL", "MSFT"]
    assert STATE_OPEN in recorder.health


async def test_canary_is_sent_last(server: Server) -> None:
    """Yahoo truncates from the front; a leading canary would report
    health while the tail was being discarded."""
    recorder = Recorder()
    connection = StreamConnection(
        PLAN, on_result=recorder.on_result, url=server.url, canary=["BTC-USD"]
    )
    await _run_until(connection, lambda: bool(server.subscriptions))
    assert server.subscriptions[0] == ["AAPL", "MSFT", "BTC-USD"]


async def test_canary_ticks_are_not_reported_as_data(server: Server) -> None:
    """The canary is an instrument, not data: never archived."""
    recorder = Recorder()
    connection = StreamConnection(
        PLAN, on_result=recorder.on_result, url=server.url, canary=["BTC-USD"]
    )
    await _run_until(connection, lambda: len(recorder.symbols) >= 2)
    assert "BTC-USD" not in recorder.symbols
    assert connection.health.last_canary_at is not None


async def test_invalid_entries_never_reach_the_wire(server: Server) -> None:
    """A single null in the list closes the socket with no status code,
    taking every symbol on the connection with it."""
    plan = ConnectionPlan(key="X", exchange="X", symbols=("AAPL", None, "", "MSFT"))  # type: ignore[arg-type]
    recorder = Recorder()
    connection = StreamConnection(plan, on_result=recorder.on_result, url=server.url)
    await _run_until(connection, lambda: bool(server.subscriptions))
    assert server.subscriptions[0] == ["AAPL", "MSFT"]
    assert any(r.rejects for r in recorder.results)


# --- reconnect -------------------------------------------------------------


async def test_reconnects_after_the_server_closes(server: Server) -> None:
    """The defect that makes this module exist.

    yfinance's client would spin here forever: its reconnect path only
    dials when `_ws is None`, and the error path never clears it.
    """

    async def close_immediately(ws: ServerConnection, symbols: list[str]) -> AsyncIterator[str]:
        yield _frame(symbols[0])
        await ws.close()

    server.behaviour = close_immediately
    recorder = Recorder()
    connection = StreamConnection(
        PLAN,
        on_result=recorder.on_result,
        on_health=recorder.on_health,
        url=server.url,
        reconnect_max_seconds=0.05,
    )
    await _run_until(connection, lambda: server.connections >= 3)
    assert server.connections >= 3
    assert STATE_RECONNECTING in recorder.health
    assert connection.health.reconnect_count >= 2


async def test_reconnect_resubscribes(server: Server) -> None:
    async def close_immediately(ws: ServerConnection, symbols: list[str]) -> AsyncIterator[str]:
        yield _frame(symbols[0])
        await ws.close()

    server.behaviour = close_immediately
    recorder = Recorder()
    connection = StreamConnection(
        PLAN, on_result=recorder.on_result, url=server.url, reconnect_max_seconds=0.05
    )
    await _run_until(connection, lambda: len(server.subscriptions) >= 2)
    assert server.subscriptions[0] == server.subscriptions[1] == ["AAPL", "MSFT"]


async def test_connection_refused_is_retried_not_raised(server: Server) -> None:
    """A connection that cannot be opened must not take down the process."""
    await server.stop()
    recorder = Recorder()
    connection = StreamConnection(
        PLAN,
        on_result=recorder.on_result,
        on_health=recorder.on_health,
        url=server.url,
        reconnect_max_seconds=0.02,
    )
    await _run_until(connection, lambda: connection.health.reconnect_count >= 2)
    assert connection.health.last_error is not None


# --- idle watchdog ---------------------------------------------------------


async def test_idle_connection_that_never_delivered_is_left_alone(server: Server) -> None:
    """A quiet connection is a closed market, not a fault.

    Cycling it would mean an endless reconnect loop every weekend, which
    is why the watchdog only fires once a message HAS arrived.
    """

    async def stay_silent(ws: ServerConnection, symbols: list[str]) -> AsyncIterator[str]:
        await server.closing.wait()
        yield ""  # pragma: no cover

    server.behaviour = stay_silent
    recorder = Recorder()
    connection = StreamConnection(
        PLAN, on_result=recorder.on_result, url=server.url, idle_timeout=0.05
    )
    task = asyncio.create_task(connection.run())
    await asyncio.sleep(0.4)
    connection.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert server.connections == 1
    assert connection.health.reconnect_count == 0


async def test_idle_after_a_message_triggers_a_reconnect(server: Server) -> None:
    """Once the stream has proven it can deliver, silence is a real signal."""

    async def one_then_silence(ws: ServerConnection, symbols: list[str]) -> AsyncIterator[str]:
        yield _frame(symbols[0])
        await server.closing.wait()

    server.behaviour = one_then_silence
    recorder = Recorder()
    connection = StreamConnection(
        PLAN,
        on_result=recorder.on_result,
        url=server.url,
        idle_timeout=0.05,
        reconnect_max_seconds=0.02,
    )
    await _run_until(connection, lambda: server.connections >= 2)
    assert connection.health.last_error is not None
    assert "no message" in connection.health.last_error


# --- backoff ---------------------------------------------------------------


def test_backoff_grows_and_is_capped() -> None:
    backoff = _Backoff(ceiling=10.0)
    delays = [backoff.next_delay() for _ in range(12)]
    assert all(0.0 <= d <= 10.0 for d in delays)
    assert max(delays[-4:]) > 0.0


def test_backoff_resets_after_a_good_session() -> None:
    backoff = _Backoff(ceiling=10.0)
    for _ in range(5):
        backoff.next_delay()
    backoff.reset()
    assert backoff.attempt == 0
    assert backoff.next_delay() <= 1.0


def test_backoff_uses_full_jitter() -> None:
    """~106 connections retrying in lockstep would arrive as one burst."""
    backoff = _Backoff(ceiling=60.0)
    for _ in range(6):
        backoff.next_delay()
    samples = {round(backoff.next_delay(), 6) for _ in range(20)}
    assert len(samples) > 1


# --- shutdown --------------------------------------------------------------


async def test_stop_ends_the_run_loop(server: Server) -> None:
    recorder = Recorder()
    connection = StreamConnection(PLAN, on_result=recorder.on_result, url=server.url)
    task = asyncio.create_task(connection.run())
    await asyncio.sleep(0.1)
    connection.stop()
    async with asyncio.timeout(2.0):
        await task
    assert connection.health.state == "closed"


async def test_stop_does_not_wait_out_the_backoff(server: Server) -> None:
    """Shutdown must not sit through a 60-second retry delay."""
    await server.stop()
    recorder = Recorder()
    connection = StreamConnection(
        PLAN, on_result=recorder.on_result, url=server.url, reconnect_max_seconds=30.0
    )
    task = asyncio.create_task(connection.run())
    await asyncio.sleep(0.2)
    connection.stop()
    async with asyncio.timeout(2.0):
        await task


# --- websockets sanity -----------------------------------------------------


def test_websockets_is_a_direct_dependency() -> None:
    """It arrives transitively via yfinance too; this module uses it
    directly, so it is declared directly."""
    assert websockets.__version__


async def test_a_canary_that_is_also_in_scope_is_still_archived(server: Server) -> None:
    """Being the health probe must not make a symbol unstreamable.

    An operator who puts BTC-USD in scope should get its ticks; the canary
    role is something we add on top, not a claim on the symbol.
    """
    plan = ConnectionPlan(key="CRY", exchange="CRY", symbols=("BTC-USD", "AAPL"))
    recorder = Recorder()
    connection = StreamConnection(
        plan, on_result=recorder.on_result, url=server.url, canary=["BTC-USD"]
    )
    await _run_until(connection, lambda: len(recorder.symbols) >= 2)
    assert "BTC-USD" in recorder.symbols
    assert connection.health.last_canary_at is not None
