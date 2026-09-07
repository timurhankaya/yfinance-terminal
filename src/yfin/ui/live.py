"""`/ui/ws` -- the browser's end of the live tick path.

The stream process publishes a committed tick to `yfin:tick:{SYMBOL}`
(`stream/publish.py`); this subscribes an open page to the symbols it is
looking at. One `redis.asyncio` pub/sub per connection: the synchronous
client the rate limiter uses is untouched, and a page watching two
symbols does not read the other nine thousand.

No identity. The terminal is public, so what guards this is the same
`RequestBrake` in front of `/ui/api` plus an Origin check -- a socket
opened from another site would otherwise read this archive with the
visitor's own network access.

Three things here are ordering decisions rather than plumbing:

**`sub` subscribes BEFORE it snapshots.** The other order has a window
between reading `live_quotes` and joining the channel, and a tick that
lands in it is lost -- the chart would sit on a stale price until the
next one. Subscribing first can only duplicate, and the page drops a
tick older than its snapshot.

**Only the sender task writes to the socket.** Frames are queued from
the read loop and from the bus reader; two tasks calling `send_json`
concurrently interleave into a frame no client can parse.

**A full queue drops the OLDEST tick.** A page that has fallen behind
wants the current price, not the one from four seconds ago, and the
`dropped` frame tells it how many it did not see.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from yfin.api.core.config import ApiSettings
from yfin.api.storage import session as api_session
from yfin.core.config import get_settings
from yfin.core.logging_setup import get_logger
from yfin.core.normalize import normalize_symbol
from yfin.stream.publish import channel
from yfin.ui import data

if TYPE_CHECKING:  # pragma: no cover - typing only
    import redis.asyncio

log = get_logger(__name__)

router = APIRouter(include_in_schema=False)

WS_PATH: Final = "/ui/ws"

#: Yahoo's own per-connection ceiling is 100 symbols; this is the page's,
#: and it is higher because a page can watch a list without the upstream
#: cost -- these are channels on our own Redis, not subscriptions to
#: Yahoo. It exists so one tab cannot ask the API to hold ten thousand.
MAX_SYMBOLS: Final = 200

#: ~40 seconds of a single busy symbol. Deeper does not help: a page that
#: is minutes behind has a problem no buffer fixes, and the `dropped`
#: frame is the honest answer.
QUEUE_MAXSIZE: Final = 1000

#: How long the bus reader blocks on a read before yielding the lock a
#: `sub` needs. It bounds subscribe latency, not tick latency.
BUS_POLL_SECONDS: Final = 0.2

#: Redis timeouts, matching `api/ratelimit/connection.py`. A hung socket
#: on this side stalls one page rather than a request.
CONNECT_TIMEOUT_SECONDS: Final = 1.0
OPERATION_TIMEOUT_SECONDS: Final = 1.0


class Op(StrEnum):
    """Every frame carries one of these under `op`, both directions."""

    Sub = "sub"
    Unsub = "unsub"
    Live = "live"
    Snap = "snap"
    Tick = "tick"
    Dropped = "dropped"
    Error = "error"


class WsError(StrEnum):
    TooMany = "too_many"
    BadSymbol = "bad_symbol"


class WsClose(IntEnum):
    """Application close codes, in the 4000-4999 private range."""

    BadOrigin = 4403


def origin_allowed(origin: str | None, host: str | None, settings: ApiSettings) -> bool:
    """Whether a socket from `origin` may read this archive.

    A WebSocket is not subject to the same-origin policy the way `fetch`
    is: any page anywhere can open one, and it would then be reading
    through the visitor's network. So the header is checked here.

    Scheme-independent on purpose. `public_base_url` is what the operator
    published and is authoritative when set; without it the request's own
    `Host` is the only thing that knows what this deployment is called,
    and a deployment behind a TLS-terminating proxy sees `http` on the
    inside while the browser sends `https`.
    """
    if not origin:
        # No Origin at all is not a browser. `wscat` and the test client
        # send none; a page always does.
        return True
    expected = settings.public_base_url or (f"//{host}" if host else "")
    if not expected:
        return False
    return urlparse(origin).netloc == urlparse(expected).netloc


def connect_bus(url: str) -> redis.asyncio.Redis:
    """The pub/sub client for one connection.

    A function of its own because it is the seam a test replaces: a
    fakeredis server can stand in for the bus, and everything above --
    the subscribe order, the queue, the frames -- is then exercised for
    real. Imported here rather than at module scope so a deployment with
    the UI off never loads `redis.asyncio`.
    """
    import redis.asyncio as aioredis

    return aioredis.Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=CONNECT_TIMEOUT_SECONDS,
        socket_timeout=OPERATION_TIMEOUT_SECONDS,
    )


def _valid_symbol(token: str) -> str | None:
    """The normalised symbol, or None when it is not one.

    Not a database check: `sub` for a symbol nobody streams is simply a
    channel that stays quiet, and asking the database per token would put
    a query behind an unauthenticated frame.
    """
    code = normalize_symbol(token)
    if not code or len(code) > 32:
        return None
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.^=-")
    return code if set(code) <= allowed else None


class LiveSession:
    """One open page. Owns its queue, its pub/sub and its symbol set."""

    def __init__(self, websocket: WebSocket, request_id: str) -> None:
        self._ws = websocket
        self._request_id = request_id
        self._out: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._symbols: set[str] = set()
        self._dropped = 0
        self._client: redis.asyncio.Redis | None = None
        self._pubsub: Any = None
        #: `get_message` and `subscribe` share one connection, so they
        #: cannot both be in flight. Held for at most BUS_POLL_SECONDS.
        self._bus = asyncio.Lock()

    # --- lifecycle ---------------------------------------------------------

    async def run(self) -> None:
        live = await self._open_bus()
        self._offer({"op": Op.Live, "enabled": live})
        sender = asyncio.create_task(self._sender())
        reader = asyncio.create_task(self._bus_reader()) if live else None
        try:
            while True:
                text = await self._ws.receive_text()
                await self._handle(text)
        except WebSocketDisconnect:
            pass
        except RuntimeError:
            # Starlette raises this when the socket is already closed;
            # there is nothing left to tell the client.
            pass
        finally:
            for task in (sender, reader):
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            await self._close_bus()

    async def _open_bus(self) -> bool:
        """True when ticks can actually flow.

        Two conditions, and the page is told which state it is in rather
        than left to infer it from silence: the stream has to be
        publishing (`yf_stream_publish_enabled`, with a URL to publish
        to), and this process has to be able to reach that Redis. Either
        one missing leaves `snap` working and the archive readable.
        """
        settings = get_settings()
        url = settings.yf_stream_publish_redis_url
        if not settings.yf_stream_publish_enabled or not url:
            return False
        try:
            client = connect_bus(url)
            await client.ping()
        except Exception as error:  # noqa: BLE001 - fail-open, like the publisher
            log.warning(
                "live bus unreachable; the terminal falls back to snapshots",
                error=str(error),
                request_id=self._request_id,
            )
            return False
        self._client = client
        self._pubsub = client.pubsub(ignore_subscribe_messages=True)
        return True

    async def _close_bus(self) -> None:
        if self._pubsub is not None:
            with contextlib.suppress(Exception):
                await self._pubsub.aclose()
            self._pubsub = None
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()
            self._client = None

    # --- frames out --------------------------------------------------------

    def _offer(self, frame: dict[str, Any]) -> None:
        """Queues a frame, dropping the oldest when the page is behind."""
        try:
            self._out.put_nowait(frame)
            return
        except asyncio.QueueFull:
            pass
        with contextlib.suppress(asyncio.QueueEmpty):
            self._out.get_nowait()
            self._dropped += 1
        with contextlib.suppress(asyncio.QueueFull):
            self._out.put_nowait(frame)

    async def _sender(self) -> None:
        """The only writer. Reports drops just before the next frame, so
        the page learns about them without a timer of its own."""
        while True:
            frame = await self._out.get()
            if self._dropped:
                dropped, self._dropped = self._dropped, 0
                await self._ws.send_json({"op": Op.Dropped, "n": dropped})
            await self._ws.send_json(frame)

    # --- frames in ---------------------------------------------------------

    async def _handle(self, text: str) -> None:
        try:
            message = json.loads(text)
        except ValueError:
            # Not JSON at all: there is no `op` to answer and no protocol
            # to fall back to. Ignored rather than closed -- a proxy that
            # injects a keepalive must not take the page's prices down.
            return
        if not isinstance(message, dict):
            return
        op = message.get("op")
        raw = message.get("symbols")
        if not isinstance(raw, list):
            return
        symbols: list[str] = []
        for token in raw:
            code = _valid_symbol(token) if isinstance(token, str) else None
            if code is None:
                self._offer({"op": Op.Error, "code": WsError.BadSymbol})
                continue
            symbols.append(code)
        if op == Op.Sub:
            await self._subscribe(symbols)
        elif op == Op.Unsub:
            await self._unsubscribe(symbols)

    async def _subscribe(self, symbols: list[str]) -> None:
        if not symbols:
            return
        if len(self._symbols | set(symbols)) > MAX_SYMBOLS:
            # The whole frame is refused rather than partly applied: a
            # page that got half its symbols would show half a watchlist
            # with nothing saying which half.
            self._offer({"op": Op.Error, "code": WsError.TooMany})
            return
        fresh = [code for code in symbols if code not in self._symbols]
        if fresh and self._pubsub is not None:
            async with self._bus:
                await self._pubsub.subscribe(*(channel(code) for code in fresh))
        self._symbols |= set(fresh)
        # After the subscribe, so no tick can land in the gap between the
        # read and the channel; the page drops any tick older than this.
        for body in await self._quotes(symbols):
            self._offer({"op": Op.Snap, "d": body})

    async def _unsubscribe(self, symbols: list[str]) -> None:
        gone = [code for code in symbols if code in self._symbols]
        if not gone:
            return
        if self._pubsub is not None:
            async with self._bus:
                await self._pubsub.unsubscribe(*(channel(code) for code in gone))
        self._symbols -= set(gone)

    async def _quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        """`live_quotes` for these symbols, off the event loop.

        The session factory is synchronous, and blocking here would stop
        every other page this worker is serving.
        """

        def read() -> list[dict[str, Any]]:
            with api_session.get_session_factory()() as session:
                return data.list_quotes(session, symbols)

        try:
            return await run_in_threadpool(read)
        except Exception as error:  # noqa: BLE001 - a missing snapshot is not fatal
            log.warning(
                "snapshot read failed", error=str(error), request_id=self._request_id
            )
            return []

    # --- the bus -----------------------------------------------------------

    async def _bus_reader(self) -> None:
        while True:
            message = None
            if self._symbols and self._pubsub is not None:
                async with self._bus:
                    try:
                        message = await self._pubsub.get_message(
                            ignore_subscribe_messages=True, timeout=BUS_POLL_SECONDS
                        )
                    except Exception as error:  # noqa: BLE001 - the page keeps its snapshot
                        log.warning(
                            "live bus read failed",
                            error=str(error),
                            request_id=self._request_id,
                        )
                        return
            if message is None:
                # Either nothing arrived or nothing is subscribed yet.
                await asyncio.sleep(0.01)
                continue
            payload = message.get("data")
            if not isinstance(payload, str):
                continue
            try:
                body = json.loads(payload)
            except ValueError:
                continue
            self._offer({"op": Op.Tick, "d": body})


@router.websocket(WS_PATH)
async def live_socket(websocket: WebSocket) -> None:
    settings: ApiSettings = websocket.app.state.api_settings
    # Generated here rather than read from request.state: a WebSocket
    # does not pass through BaseHTTPMiddleware, so RequestContextMiddleware
    # never ran and there is no id to inherit.
    request_id = uuid4().hex
    if not origin_allowed(
        websocket.headers.get("origin"), websocket.headers.get("host"), settings
    ):
        # Accepted first so the browser sees the close CODE. A refused
        # handshake surfaces as a bare "connection failed" and the page
        # cannot tell a misconfiguration from an outage.
        await websocket.accept()
        await websocket.close(code=WsClose.BadOrigin)
        return
    await websocket.accept()
    await LiveSession(websocket, request_id).run()
