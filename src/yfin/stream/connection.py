"""One upstream WebSocket connection, kept alive.

This replaces `yfinance.WebSocket` / `AsyncWebSocket` rather than wrapping
them, for reasons that are all defects in the upstream client (verified
against yfinance 1.7.0, `live.py`):

  * `AsyncWebSocket`'s reconnect is dead code. Its `except` branch calls
    `_connect()`, but `_connect()` only dials when `self._ws is None` and
    the error path never sets it back to None -- so a dropped socket
    produces an infinite error loop and never reconnects.
  * The synchronous `WebSocket.listen()` simply `break`s out of its loop
    on any exception and stops, silently.
  * Whether exceptions are swallowed depends on `YfConfig.debug.hide_exceptions`,
    a library-wide flag, and `verbose=True` prints to stdout by default.
  * It re-sends the full subscription every 15 seconds, which is both
    unnecessary (a connection stays open for at least 4 minutes with no
    traffic -- measured) and harmful: every re-send re-applies Yahoo's
    100-symbol truncation.

The server never reports errors. Not for an invalid symbol, not for a
subscription past the quota, not for a dropped subscription. So this class
cannot detect trouble by listening for it; it watches for *silence*
instead, in three independent ways -- protocol ping/pong, an idle
watchdog, and a canary symbol at the end of the subscription list.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Protocol

from yfin.core.logging_setup import get_logger
from yfin.stream.protocol import DecodeResult, Reject, decode_envelope, validate_subscription
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
    """The slice of `websockets` this module uses.

    Narrow on purpose: the loopback tests supply their own implementation,
    and a reconnect that is only exercised against a mock is a reconnect
    that has never been tested.
    """

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
    """Full-jitter exponential backoff.

    Full jitter rather than plain doubling: ~106 connections reconnecting
    after a network blip would otherwise retry in lockstep and arrive as
    one burst.
    """

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
    """Keeps one subscription alive until it is told to stop.

    Never raises out of `run()`: a connection dying must take down its own
    symbols and nothing else, so every error is recorded and retried. The
    only way out is `stop()`.
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

        Validation is not politeness. Measured: a malformed frame closes
        the connection with no status code and no message -- a bare string
        instead of a list, an unknown action key, or a single `null` in
        the list each killed the socket. One bad entry therefore takes
        down every symbol on this connection, and there is no error to
        observe afterwards.
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

        The stop signal is awaited alongside the read rather than checked
        between reads. `recv()` blocks until a message arrives, so a
        loop-top check alone would leave a stop request waiting out the
        idle timeout -- five minutes by default -- on a quiet connection.
        """
        stop_wait = asyncio.ensure_future(self._stopping.wait())
        try:
            while not self._stopping.is_set():
                read = asyncio.ensure_future(ws.recv())
                done, _ = await asyncio.wait(
                    {read, stop_wait},
                    timeout=self._idle_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_wait in done:
                    read.cancel()
                    return
                if read not in done:
                    read.cancel()
                    # A connection that has never delivered anything is
                    # not evidence of a problem -- it is a closed market.
                    # Cycling it would mean an endless reconnect loop
                    # every weekend. Once it HAS delivered, silence is a
                    # real signal.
                    if self.health.last_message_at is None:
                        continue
                    raise _IdleTimeout(f"no message for {self._idle_timeout:.0f}s")
                # Re-raises whatever killed the socket, so the outer loop
                # can record it and reconnect.
                self._handle(read.result())
        finally:
            stop_wait.cancel()

    def _handle(self, raw: str | bytes) -> None:
        now = datetime.now(UTC)
        self.health.last_message_at = now
        result = decode_envelope(raw, received_at=now)

        symbol = result.row.get("symbol") if result.row else None
        if symbol is not None and symbol in self._canary_set:
            # The canary is an instrument, not data: it is never archived
            # and never appears in stream_scope. Its only job is to go
            # quiet when the subscription is truncated or dropped, which
            # is the only way to notice either.
            self.health.last_canary_at = now
            self._emit_health()
            return

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


async def _default_connector(url: str) -> WebSocketLike:
    """The real client.

    Imported lazily so the pure-logic tests do not need `websockets`
    installed to import this module, and so a connection failure surfaces
    here rather than at import time.
    """
    from websockets.asyncio.client import connect

    return await connect(
        url,
        ping_interval=PING_INTERVAL,
        ping_timeout=PING_TIMEOUT,
        max_size=None,
    )


def reject_for_subscription(symbols: Sequence[str]) -> list[Reject]:
    """Convenience for callers that want to inspect a list before sending."""
    _, rejects = validate_subscription(list(symbols))
    return rejects
