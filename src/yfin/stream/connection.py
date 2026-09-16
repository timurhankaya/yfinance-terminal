"""One upstream WebSocket connection, kept alive.

The server never reports errors, so trouble is detected as silence:
ping/pong, an idle watchdog, and a canary at the end of the subscription.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from yfin.core.logging_setup import get_logger
from yfin.stream.protocol import decode_envelope, validate_subscription
from yfin.stream.rejects import DecodeResult
from yfin.stream.topology import ConnectionPlan

log = get_logger(__name__)

YAHOO_STREAM_URL: Final = "wss://streamer.finance.yahoo.com/?version=2"

#: websockets' own keepalive. Catches the case where TCP is still up but
#: the peer is gone -- the one failure the idle watchdog cannot tell from
#: a quiet market.
PING_INTERVAL: Final = 20.0
PING_TIMEOUT: Final = 20.0

STATE_CONNECTING: Final = "connecting"
STATE_OPEN: Final = "open"
STATE_RECONNECTING: Final = "reconnecting"
STATE_CLOSED: Final = "closed"


class WebSocketLike(Protocol):
    """The slice of `websockets` this module uses; loopback tests supply their own."""

    async def send(self, message: str) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self) -> None: ...


Connector = Callable[[str], Awaitable[WebSocketLike]]


@dataclass
class ConnectionHealth:
    """What `yfin stream status` reads, per connection."""

    connection_key: str
    state: str = STATE_CONNECTING
    subscribed_count: int = 0
    connected_at: datetime | None = None
    last_message_at: datetime | None = None
    last_canary_at: datetime | None = None
    reconnect_count: int = 0
    last_error: str | None = None


@dataclass
class _Backoff:
    """Full-jitter exponential backoff, so reconnecting connections do not retry in lockstep."""

    ceiling: float
    base: float = 1.0
    attempt: int = 0
    _random: random.Random = field(default_factory=random.Random)

    def next_delay(self) -> float:
        self.attempt += 1
        window = min(self.ceiling, self.base * (2 ** (self.attempt - 1)))
        return self._random.uniform(0.0, window)

    def reset(self) -> None:
        self.attempt = 0


class _IdleTimeout(Exception):
    """No message for longer than the watchdog allows."""


class StreamConnection:
    """Keeps one subscription alive until `stop()`.

    Never raises out of `run()`: a dying connection must take down its own
    symbols and nothing else, so every error is recorded and retried.
    """

    def __init__(
        self,
        plan: ConnectionPlan,
        *,
        on_result: Callable[[DecodeResult], None],
        on_health: Callable[[ConnectionHealth], None] | None = None,
        canary: Sequence[str] = (),
        url: str = YAHOO_STREAM_URL,
        idle_timeout: float = 300.0,
        reconnect_max_seconds: float = 60.0,
        connector: Connector | None = None,
    ) -> None:
        self.plan = plan
        self.health = ConnectionHealth(connection_key=plan.key)
        self._on_result = on_result
        self._on_health = on_health
        self._canary = tuple(canary)
        self._canary_set = frozenset(canary)
        self._plan_symbols = frozenset(plan.symbols)
        self._url = url
        self._idle_timeout = idle_timeout
        self._backoff = _Backoff(ceiling=reconnect_max_seconds)
        self._connector = connector or _default_connector
        self._stopping = asyncio.Event()
        self._ws: WebSocketLike | None = None

    # --- public API --------------------------------------------------------

    async def run(self) -> None:
        """Connect, subscribe, read, and reconnect forever."""
        while not self._stopping.is_set():
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except _IdleTimeout as exc:
                self._note_failure(str(exc))
            except Exception as exc:  # noqa: BLE001 - deliberately total
                self._note_failure(f"{type(exc).__name__}: {exc}")
            if self._stopping.is_set():
                break
            await self._sleep_backoff()
        self._set_state(STATE_CLOSED)

    def stop(self) -> None:
        self._stopping.set()

    def subscription(self) -> tuple[str, ...]:
        """What goes on the wire, canary last (see ConnectionPlan)."""
        return self.plan.subscription(self._canary)

    # --- internals ---------------------------------------------------------

    async def _session(self) -> None:
        self._set_state(STATE_CONNECTING)
        ws = await self._connector(self._url)
        self._ws = ws
        try:
            await self._subscribe(ws)
            self.health.connected_at = datetime.now(UTC)
            # Reset only after a successful subscribe: a socket that opens
            # and immediately fails is still a failure, and resetting on
            # connect alone would turn the backoff into a tight loop.
            self._backoff.reset()
            self._set_state(STATE_OPEN)
            await self._read_forever(ws)
        finally:
            self._ws = None
            # Closing an already-broken socket is expected, not notable.
            with contextlib.suppress(Exception):
                await ws.close()

    async def _subscribe(self, ws: WebSocketLike) -> None:
        """Sends the subscription, after validating every entry.

        One malformed entry closes the socket with no status code or
        message, taking down every symbol on this connection.
        """
        symbols, rejects = validate_subscription(list(self.subscription()))
        for reject in rejects:
            self._report(DecodeResult(rejects=[reject]))
        if not symbols:
            # An empty list is accepted by the server but pointless; treat
            # it as a connection with nothing to do rather than spinning.
            raise _IdleTimeout("no valid symbols to subscribe")
        await ws.send(json.dumps({"subscribe": symbols}))
        self.health.subscribed_count = len(symbols)
        log.info("stream subscribed", connection=self.plan.key, symbols=len(symbols))

    async def _read_forever(self, ws: WebSocketLike) -> None:
        """Reads until told to stop, the socket dies, or it goes quiet.

        The stop signal is awaited alongside the blocking `recv()`; a
        loop-top check would leave a stop waiting out the idle timeout.
        """
        stop_wait = _spawn(self._stopping.wait())
        try:
            while not self._stopping.is_set():
                read = _spawn(ws.recv())
                done, _ = await asyncio.wait(
                    {read, stop_wait},
                    timeout=self._idle_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_wait in done:
                    await _discard(read)
                    return
                if read not in done:
                    await _discard(read)
                    # Silence before the first message is a closed market,
                    # not a fault; cycling it would reconnect endlessly.
                    if self.health.last_message_at is None:
                        continue
                    raise _IdleTimeout(f"no message for {self._idle_timeout:.0f}s")
                # Re-raises whatever killed the socket, so the outer loop
                # can record it and reconnect.
                self._handle(read.result())
        finally:
            await _discard(stop_wait)

    def _handle(self, raw: str | bytes) -> None:
        now = datetime.now(UTC)
        self.health.last_message_at = now
        result = decode_envelope(raw, received_at=now)

        symbol = result.row.get("symbol") if result.row else None
        if symbol is not None and symbol in self._canary_set:
            # The canary's job is to go quiet when the subscription is
            # truncated or dropped -- the only way to notice either, since
            # the server never reports it.
            self.health.last_canary_at = now
            self._emit_health()
            if symbol not in self._plan_symbols:
                # Pure instrument: appended by us, not asked for, so it is
                # not data and is not archived.
                return
            # A canary that is also in scope is data too, and falls through.

        self._report(result)

    def _report(self, result: DecodeResult) -> None:
        if result.row is not None or result.rejects:
            self._on_result(result)

    def _note_failure(self, message: str) -> None:
        self.health.last_error = message
        self.health.reconnect_count += 1
        self._set_state(STATE_RECONNECTING)
        log.warning("stream connection lost", connection=self.plan.key, error=message)

    async def _sleep_backoff(self) -> None:
        delay = self._backoff.next_delay()
        # Waiting on the stop event rather than sleeping outright, so a
        # stop request does not have to sit through a 60-second backoff.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=delay)

    def _set_state(self, state: str) -> None:
        self.health.state = state
        self._emit_health()

    def _emit_health(self) -> None:
        if self._on_health is not None:
            self._on_health(self.health)


def _consume_result(task: asyncio.Future[Any]) -> None:
    """Reads a finished task's exception so asyncio does not log it.

    A done-callback, not an await in `finally`: when the connection task
    is cancelled, `finally` never reaches the pending read.
    """
    if not task.cancelled():
        task.exception()


def _spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
    task = asyncio.ensure_future(coro)
    task.add_done_callback(_consume_result)
    return task


async def _discard(task: asyncio.Future[Any]) -> None:
    """Cancels a task and waits for it to finish unwinding."""
    task.cancel()
    with contextlib.suppress(BaseException):
        await task


async def _default_connector(url: str) -> WebSocketLike:
    """The real client; imported lazily so importing this module needs no `websockets`."""
    from websockets.asyncio.client import connect

    return await connect(
        url,
        ping_interval=PING_INTERVAL,
        ping_timeout=PING_TIMEOUT,
        max_size=None,
    )

