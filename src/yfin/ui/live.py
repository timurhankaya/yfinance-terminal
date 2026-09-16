"""`/ui/ws`: subscribes an open page to `yfin:tick:{SYMBOL}` via its own
`redis.asyncio` pub/sub. No identity; an Origin check and this module's
own connection limits guard it (a WebSocket never passes `RequestBrake`).
Invariants: `sub` subscribes before it snapshots (a tick in between would
be lost); only the sender task writes; a full queue drops the OLDEST."""

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
from yfin.core import metrics
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

#: Deeper does not help: a page minutes behind has a problem no buffer
#: fixes, and the `dropped` frame is the honest answer.
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
    """Application close codes. 4000-4999 is what a browser hands back to the
    page unchanged; the page must tell "never retry" from "retry later"."""

    #: The Origin is not this deployment. Permanent: do not reconnect.
    BadOrigin = 4403
    #: The process is at its socket ceiling, or this address opened too
    #: many too quickly. Temporary: reconnect with backoff.
    TooBusy = 4429


#: New sockets per address per minute, and how many are open right now;
#: both per process, protecting this worker's threadpool and database
#: pool. A plain int suffices: check and increment happen on the one
#: event loop with no await between them.
_handshakes = FixedWindow()
_open_sessions = 0


def connect_bus(url: str) -> redis.asyncio.Redis:
    """The pub/sub client for one connection; the seam a test replaces with
    fakeredis. Imported here so a deployment with the UI off never loads
    `redis.asyncio`."""
    import redis.asyncio as aioredis

    return aioredis.Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=CONNECT_TIMEOUT_SECONDS,
        socket_timeout=OPERATION_TIMEOUT_SECONDS,
    )


def _valid_symbol(token: str) -> str | None:
    """The normalised symbol, or None when it is not one. Not a database
    check: that would put a query behind an unauthenticated frame."""
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
        """True when ticks can actually flow: the stream is publishing and
        this process can reach that Redis. Either missing leaves `snap`
        working, and the page is told rather than left to infer it."""
        settings = get_settings()
        url = settings.yf_stream_publish_redis_url
        if not settings.yf_stream_publish_enabled or not url:
            return False
        try:
            client = connect_bus(url)
            await client.ping()
        except Exception as error:  # noqa: BLE001 - fail-open, like the publisher
            log.error(
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
        """The bus failed mid-session: say so, or the page keeps
        `live.enabled=true` and never falls back to `snap` refreshes.
        Synchronous because it is called inside the pub/sub lock; the
        connection is closed once, in `_close_bus`."""
        if not self._bus_ok:
            return
        self._bus_ok = False
        log.error(
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
        """The drop count, zeroed in the same breath. Read-and-reset must not
        straddle an `await`: a drop landing mid-send belongs to the next
        report."""
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
                    # Redis went away since `_open_bus`; the socket must
                    # outlive the bus so the archive stays readable.
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
            log.error(
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
                        # The frame is what lets the page fall back to
                        # snapshots instead of trusting a dead feed.
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
    Origin alone is not a limit: a client sending none is admitted by
    design, so the connection limits are what bound a loop of them."""
    if not origin_allowed(
        websocket.headers.get("origin"), websocket.headers.get("host"), settings
    ):
        return WsClose.BadOrigin
    # The networks are parsed per connection rather than cached: it is a
    # handful of `ip_network` calls against the cost of a socket, and a
    # cache here would be a second place `trusted_proxies` is resolved.
    client_ip = resolve_client_ip(websocket, trusted_networks(settings))
    if not _handshakes.allow(client_ip, settings.ui_ws_connections_per_minute):
        metrics.inc("yfin_ui_ws_refusals_total", reason="rate")
        log.debug("ui_ws_handshake_limited", client_ip=client_ip, request_id=request_id)
        return WsClose.TooBusy
    if _open_sessions >= settings.ui_ws_max_connections:
        # The casualty of an exhausted threadpool or database pool is not
        # this free terminal but `/v1`, in the same worker.
        metrics.inc("yfin_ui_ws_refusals_total", reason="capacity")
        log.debug("ui_ws_at_capacity", open_sessions=_open_sessions, request_id=request_id)
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
