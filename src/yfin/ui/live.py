"""`/ui/ws` -- the browser's end of the live tick path.

The stream process publishes a committed tick to `yfin:tick:{SYMBOL}`
(`stream/publish.py`); this subscribes an open page to the symbols it is
looking at. One `redis.asyncio` pub/sub per connection: the synchronous
client the rate limiter uses is untouched, and a page watching two
symbols does not read the other nine thousand.

No identity. The terminal is public, so what guards this is an Origin
check -- a socket opened from another site would otherwise read this
archive with the visitor's own network access -- plus this module's own
limits, because `RequestBrake` is a `BaseHTTPMiddleware` and a WebSocket
never passes through one.

**Close codes are the protocol's other half.** The socket is accepted
before it is closed precisely so the browser is handed a code, and each
one means a different thing to the page:

- `4403` (`WsClose.BadOrigin`): the Origin is not this deployment. This
  will never work -- a wrong `public_base_url`, or a proxy rewriting
  `Host` -- so the page must NOT reconnect; it should surface the
  misconfiguration instead of retrying forever behind "connecting".
- `4429` (`WsClose.TooBusy`): this process is at its socket ceiling, or
  this address opened too many too fast. Temporary: reconnect, with
  backoff.

Anything else (a normal `1000`, `1006` from a dropped connection) is an
ordinary outage and reconnects as before.

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
from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from yfin.api.core.config import ApiSettings
from yfin.api.core.middleware import resolve_client_ip, trusted_networks
from yfin.api.core.origin import origin_allowed
from yfin.api.ratelimit.fixed_window import FixedWindow
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

#: An ESTIMATE, not a measurement: roughly 40 seconds of one busy symbol
#: IF a busy symbol ticks about twice a second. The tick rate for US
#: equities is one of the numbers `docs/measurements/websocket.md` marks
#: as not measured -- the stream capture was taken on a Sunday -- and it
#: "must be repeated during open market hours before any capacity claim
#: about equities is made". This constant is that claim, so it stands as
#: an estimate pending that run.
#:
#: What does not depend on the rate: deeper does not help. A page minutes
#: behind has a problem no buffer fixes, and the `dropped` frame is the
#: honest answer.
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
    """Application close codes, in the 4000-4999 private range.

    4000-4999 is what a browser hands back to the page unchanged;
    anything below is reserved and some browsers rewrite it. The module
    docstring says what each one asks the page to do -- the point of a
    code is that "never retry" and "retry later" are different states,
    and a page that cannot tell them apart shows "connecting" forever.
    """

    #: The Origin is not this deployment. Permanent: do not reconnect.
    BadOrigin = 4403
    #: The process is at its socket ceiling, or this address opened too
    #: many too quickly. Temporary: reconnect with backoff.
    TooBusy = 4429


#: New sockets per address per minute, and how many are open right now.
#: Both are per PROCESS and in-process, the same kind of crude brake
#: `RequestBrake` is rather than a cluster-wide limit: what they protect
#: is this worker's threadpool and database pool, which are also `/v1`'s.
#:
#: A plain int is enough for the count: every socket is admitted, run and
#: released on the one event loop, and the check and the increment happen
#: with no await between them, so two handshakes cannot both pass a
#: ceiling that only one of them fits under.
_handshakes = FixedWindow()
_open_sessions = 0


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
        #: False once the bus has failed. The pub/sub object is kept so
        #: `_close_bus` can still close it; this is what every user of it
        #: checks, so one failure takes the whole live path down together
        #: rather than leaving half of it running.
        self._bus_ok = False
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
        self._bus_ok = True
        return True

    def _go_dark(self, error: Exception) -> None:
        """The bus failed mid-session: say so, and stop using it.

        Fail-open is the rule on every layer of the live path -- the
        publisher applies it, and open time applies it -- and this is the
        third place it has to hold. Without it the page keeps the
        `live.enabled=true` it was told when the socket opened, keeps
        showing the last tick it received, and never falls back to the
        periodic `snap` refresh: a price that stopped moving looks
        exactly like a quiet market.

        Synchronous, because it is called from inside the pub/sub lock
        and must not await there. The connection is closed once, in
        `_close_bus`.
        """
        if not self._bus_ok:
            return
        self._bus_ok = False
        log.warning(
            "live bus lost; the terminal falls back to snapshots",
            error=str(error),
            request_id=self._request_id,
        )
        self._offer({"op": Op.Live, "enabled": False})

    async def _close_bus(self) -> None:
        self._bus_ok = False
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
        # Bare, not suppressed: the `get_nowait` above just freed a slot
        # and one task owns this queue, so a QueueFull here would mean
        # that invariant broke. Suppressing it would drop the frame
        # WITHOUT counting it -- a hole in the one counter that exists to
        # make holes visible.
        self._out.put_nowait(frame)

    def _take_dropped(self) -> int:
        """The drop count, zeroed in the same breath.

        Its own method because read-and-reset must not straddle an
        `await`: a drop that lands while the `dropped` frame is being
        sent belongs to the NEXT report, and if the reset happened after
        the send it would be lost instead. Nothing here awaits, so the
        two halves cannot be separated by a later edit without deleting
        this paragraph.
        """
        count, self._dropped = self._dropped, 0
        return count

    async def _sender(self) -> None:
        """The only writer. Reports drops just before the next frame, so
        the page learns about them without a timer of its own."""
        while True:
            frame = await self._out.get()
            dropped = self._take_dropped()
            try:
                if dropped:
                    await self._ws.send_json({"op": Op.Dropped, "n": dropped})
                await self._ws.send_json(frame)
            except (WebSocketDisconnect, RuntimeError):
                # The browser can close while this task is awaiting a send.
                # Treat that as normal lifecycle completion; otherwise the
                # task exception is reported as an ASGI application error.
                return

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
        if len(raw) > MAX_SYMBOLS:
            # Refused BEFORE the list is walked. uvicorn's `ws_max_size`
            # is 16 MB, so one frame from an unauthenticated client can
            # carry on the order of a million tokens; normalising all of
            # them only to refuse the frame afterwards is exactly the
            # work that makes such a frame worth sending.
            self._offer({"op": Op.Error, "code": WsError.TooMany})
            return
        symbols: list[str] = []
        rejected = False
        for token in raw:
            code = _valid_symbol(token) if isinstance(token, str) else None
            if code is None:
                rejected = True
                continue
            symbols.append(code)
        if rejected:
            # One frame per received frame, not one per bad token: ten
            # identical frames tell the page nothing the first did not,
            # and it cannot tell them apart anyway.
            self._offer({"op": Op.Error, "code": WsError.BadSymbol})
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
        if fresh and self._bus_ok and self._pubsub is not None:
            async with self._bus:
                try:
                    await self._pubsub.subscribe(*(channel(code) for code in fresh))
                except Exception as error:  # noqa: BLE001 - fail-open, like the publisher
                    # Redis went away since `_open_bus`. Unguarded, this
                    # left `_handle` and `run`'s handler catches only
                    # WebSocketDisconnect and RuntimeError, so it killed
                    # the socket -- taking the archive down with the bus.
                    self._go_dark(error)
        self._symbols |= set(fresh)
        # After the subscribe, so no tick can land in the gap between the
        # read and the channel; the page drops any tick older than this.
        for body in await self._quotes(symbols):
            self._offer({"op": Op.Snap, "d": body})

    async def _unsubscribe(self, symbols: list[str]) -> None:
        gone = [code for code in symbols if code in self._symbols]
        if not gone:
            return
        if self._bus_ok and self._pubsub is not None:
            async with self._bus:
                try:
                    await self._pubsub.unsubscribe(*(channel(code) for code in gone))
                except Exception as error:  # noqa: BLE001 - fail-open, like the publisher
                    self._go_dark(error)
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
        # `_bus_ok` rather than True: a subscribe that failed elsewhere
        # has already told the page the feed is off, and a reader still
        # polling a dead pub/sub would be the only part of the session
        # that had not heard.
        while self._bus_ok:
            message = None
            if self._symbols and self._pubsub is not None:
                async with self._bus:
                    try:
                        message = await self._pubsub.get_message(
                            ignore_subscribe_messages=True, timeout=BUS_POLL_SECONDS
                        )
                    except Exception as error:  # noqa: BLE001 - the page keeps its snapshot
                        # Returning quietly used to leave the page
                        # believing `live.enabled=true` with no reader
                        # behind it; the frame is what lets it fall back.
                        self._go_dark(error)
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


def _refusal(websocket: WebSocket, settings: ApiSettings, request_id: str) -> WsClose | None:
    """The close code this socket is refused with, or None to serve it.

    The Origin check answers "may this page read the archive"; the two
    limits answer "can this process afford another socket". Origin is
    not a limit on its own -- a client that sends no Origin is admitted
    by design, so a loop of them would otherwise be admitted without
    end.
    """
    if not origin_allowed(
        websocket.headers.get("origin"), websocket.headers.get("host"), settings
    ):
        return WsClose.BadOrigin
    # The networks are parsed per connection rather than cached: it is a
    # handful of `ip_network` calls against the cost of a socket, and a
    # cache here would be a second place `trusted_proxies` is resolved.
    client_ip = resolve_client_ip(websocket, trusted_networks(settings))
    if not _handshakes.allow(client_ip, settings.ui_ws_connections_per_minute):
        log.warning("ui_ws_handshake_limited", client_ip=client_ip, request_id=request_id)
        return WsClose.TooBusy
    if _open_sessions >= settings.ui_ws_max_connections:
        # The casualty of an exhausted threadpool or database pool is not
        # this free terminal but `/v1`, in the same worker.
        log.warning("ui_ws_at_capacity", open_sessions=_open_sessions, request_id=request_id)
        return WsClose.TooBusy
    return None


@router.websocket(WS_PATH)
async def live_socket(websocket: WebSocket) -> None:
    global _open_sessions

    settings: ApiSettings = websocket.app.state.api_settings
    # Generated here rather than read from request.state: a WebSocket
    # does not pass through BaseHTTPMiddleware, so RequestContextMiddleware
    # never ran and there is no id to inherit.
    request_id = uuid4().hex
    refused = _refusal(websocket, settings, request_id)
    if refused is not None:
        # Accepted first so the browser sees the close CODE. A refused
        # handshake surfaces as a bare "connection failed" and the page
        # cannot tell a misconfiguration from an outage, nor a permanent
        # refusal from a temporary one.
        await websocket.accept()
        await websocket.close(code=refused)
        return
    # Counted BEFORE the handshake is completed: `accept()` awaits, and
    # a ceiling checked on one side of an await while the count is taken
    # on the other is a ceiling every concurrent handshake passes.
    _open_sessions += 1
    try:
        await websocket.accept()
        await LiveSession(websocket, request_id).run()
    finally:
        _open_sessions -= 1
