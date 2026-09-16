"""`/ui/ws`: the origin gate, the frame protocol and the fallback when the live bus is not
available (the default, since `yf_stream_publish_enabled` is off), plus who may open a
socket, what a bad frame does and the per-connection ceiling. No Redis, no database; the
pub/sub half is covered against fakeredis in tests/repo."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from yfin.api.core.config import ApiSettings
from yfin.api.ratelimit.fixed_window import FixedWindow
from yfin.ui import live
from yfin.ui.live import (
    MAX_SYMBOLS,
    QUEUE_MAXSIZE,
    LiveSession,
    Op,
    WsClose,
    WsError,
    origin_allowed,
)

KEY = "k" * 32


def settings(**overrides: Any) -> ApiSettings:
    return ApiSettings(
        _env_file=None,
        jwt_signing_key=KEY,
        jwt_kid="k1",
        jwt_issuer="yfin-api",
        ui_enabled=True,
        **overrides,
    )


@pytest.fixture(autouse=True)
def _fresh_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """The handshake window and the open-socket count are per PROCESS, so
    one test's sockets would otherwise be another's ceiling."""
    monkeypatch.setattr(live, "_handshakes", FixedWindow())
    monkeypatch.setattr(live, "_open_sessions", 0)


@pytest.fixture
def quotes() -> list[dict[str, Any]]:
    """What the stubbed `live_quotes` read returns."""
    return []


@pytest.fixture
def overrides() -> dict[str, Any]:
    """Settings a test wants changed; parametrized where it matters."""
    return {}


@pytest.fixture
def stub_quotes(monkeypatch: pytest.MonkeyPatch, quotes: list[dict[str, Any]]) -> None:
    async def read(self: Any, symbols: list[str]) -> list[dict[str, Any]]:
        # Stubbed at the session, not at `data.list_quotes`: the real one
        # would build an engine and dial PostgreSQL, and what is under
        # test here is the frame order, not the query.
        return [body for body in quotes if body["s"] in symbols]

    monkeypatch.setattr(live.LiveSession, "_quotes", read)


@pytest.fixture
def client(
    tmp_path: Path,
    overrides: dict[str, Any],
    stub_quotes: None,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    from yfin.api.app import create_app
    from yfin.ui import pages

    monkeypatch.setattr(pages, "default_dist_dir", lambda: tmp_path / "absent")
    with TestClient(create_app(settings(**overrides))) as test_client:
        yield test_client


class TestOriginGate:
    """A WebSocket is not subject to the same-origin policy, so any page
    anywhere could otherwise read this archive through the visitor."""

    def test_no_origin_is_allowed(self) -> None:
        """`wscat` and the test client send none; a browser always does."""
        assert origin_allowed(None, "yfin.example", settings()) is True

    def test_the_request_host_is_the_default_expectation(self) -> None:
        assert origin_allowed("http://yfin.example", "yfin.example", settings()) is True

    def test_the_scheme_does_not_have_to_match(self) -> None:
        """Behind a TLS-terminating proxy the app sees http on the inside
        while the browser sends https."""
        assert origin_allowed("https://yfin.example", "yfin.example", settings()) is True

    def test_another_site_is_refused(self) -> None:
        assert origin_allowed("https://evil.example", "yfin.example", settings()) is False

    def test_the_published_url_wins_over_the_host_header(self) -> None:
        """`Host` is attacker-controllable behind a permissive proxy;
        `public_base_url` is what the operator configured."""
        configured = settings(public_base_url="https://yfin.example")
        assert origin_allowed("http://anything.example", "anything.example", configured) is False
        assert origin_allowed("https://yfin.example", "anything.example", configured) is True

    def test_a_port_is_part_of_the_identity(self) -> None:
        assert origin_allowed("http://localhost:5173", "localhost:8000", settings()) is False

    def test_the_socket_closes_with_the_code(self, client: TestClient) -> None:
        """Accepted first, then closed: a refused handshake reaches the
        page as a bare "connection failed" with nothing to act on."""
        connect = client.websocket_connect("/ui/ws", headers={"origin": "https://evil.example"})
        with connect as socket, pytest.raises(WebSocketDisconnect) as refused:
            socket.receive_json()
        assert refused.value.code == WsClose.BadOrigin

    def test_the_socket_stays_open_for_its_own_origin(self, client: TestClient) -> None:
        with client.websocket_connect(
            "/ui/ws", headers={"origin": "http://testserver"}
        ) as socket:
            assert socket.receive_json()["op"] == Op.Live


class TestFrames:
    def test_the_first_frame_says_whether_ticks_will_flow(self, client: TestClient) -> None:
        """`yf_stream_publish_enabled` is off by default, so a fresh
        deployment gets `enabled: false` and the page says so instead of
        showing a price that silently never updates."""
        with client.websocket_connect("/ui/ws") as socket:
            assert socket.receive_json() == {"op": Op.Live, "enabled": False}

    @pytest.mark.parametrize("quotes", [[{"s": "AAPL", "t": 1, "p": "232.35", "mh": 1}]])
    def test_sub_answers_with_a_snapshot(self, client: TestClient) -> None:
        """With the bus down the snapshot is all there is, and it is what
        keeps the strip showing a real last price."""
        with client.websocket_connect("/ui/ws") as socket:
            socket.receive_json()
            socket.send_json({"op": Op.Sub, "symbols": ["aapl"]})
            assert socket.receive_json() == {
                "op": Op.Snap,
                "d": {"s": "AAPL", "t": 1, "p": "232.35", "mh": 1},
            }

    def test_a_symbol_that_is_not_one_is_named(self, client: TestClient) -> None:
        with client.websocket_connect("/ui/ws") as socket:
            socket.receive_json()
            socket.send_json({"op": Op.Sub, "symbols": ["../etc/passwd"]})
            assert socket.receive_json() == {"op": Op.Error, "code": WsError.BadSymbol}

    def test_ten_bad_symbols_are_one_frame_not_ten(self, client: TestClient) -> None:
        """Ten identical frames tell the page nothing the first did not,
        and it cannot tell them apart anyway."""
        with client.websocket_connect("/ui/ws") as socket:
            socket.receive_json()
            socket.send_json({"op": Op.Sub, "symbols": ["../etc/passwd"] * 10 + ["AAPL"]})
            assert socket.receive_json() == {"op": Op.Error, "code": WsError.BadSymbol}
            # The valid one still went through: the next frame is its
            # snapshot request's answer, not a second bad_symbol.
            socket.send_json({"op": Op.Unsub, "symbols": ["AAPL"]})

    def test_too_many_symbols_refuses_the_whole_frame(self, client: TestClient) -> None:
        """Half a watchlist with nothing saying which half is worse than
        none of it."""
        with client.websocket_connect("/ui/ws") as socket:
            socket.receive_json()
            symbols = [f"S{index}" for index in range(MAX_SYMBOLS + 1)]
            socket.send_json({"op": Op.Sub, "symbols": symbols})
            assert socket.receive_json() == {"op": Op.Error, "code": WsError.TooMany}

    def test_an_oversized_frame_is_refused_before_it_is_walked(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """uvicorn's `ws_max_size` is 16 MB, so one frame from an
        unauthenticated client can carry ~10^6 tokens. Normalising them
        all only to refuse the frame afterwards is exactly the work that
        makes such a frame worth sending."""
        walked = 0

        def counted(token: str) -> str | None:
            nonlocal walked
            walked += 1
            return live._valid_symbol(token)

        monkeypatch.setattr(live, "_valid_symbol", counted)
        with client.websocket_connect("/ui/ws") as socket:
            socket.receive_json()
            symbols = [f"S{index}" for index in range(MAX_SYMBOLS * 10)]
            socket.send_json({"op": Op.Sub, "symbols": symbols})
            assert socket.receive_json() == {"op": Op.Error, "code": WsError.TooMany}
        assert walked == 0

    def test_a_frame_that_is_not_json_is_ignored(self, client: TestClient) -> None:
        """A proxy that injects a keepalive must not take the prices down."""
        with client.websocket_connect("/ui/ws") as socket:
            socket.receive_json()
            socket.send_text("ping")
            socket.send_json({"op": Op.Sub, "symbols": ["AAPL"]})
            # Still answering: the connection survived the junk frame.
            socket.send_json({"op": Op.Unsub, "symbols": ["AAPL"]})

    def test_unsub_for_something_never_subscribed_is_a_no_op(self, client: TestClient) -> None:
        with client.websocket_connect("/ui/ws") as socket:
            socket.receive_json()
            socket.send_json({"op": Op.Unsub, "symbols": ["AAPL"]})
            socket.send_json({"op": Op.Sub, "symbols": ["MSFT"]})


class TestClosedCodes:
    @pytest.mark.parametrize("code", list(WsClose))
    def test_every_close_code_is_in_the_private_range(self, code: WsClose) -> None:
        """4000-4999 is what a browser hands back to the page; anything
        below 4000 is reserved and some browsers rewrite it."""
        assert 4000 <= code <= 4999

    def test_the_codes_are_distinct(self) -> None:
        """The page acts on them differently -- 4403 must never be
        retried, 4429 must be -- which it can only do if they differ."""
        assert len({code.value for code in WsClose}) == len(list(WsClose))

    def test_the_origin_check_is_the_shared_one(self) -> None:
        """The admin page's CSRF guard compares origins the same way. Two
        copies of this comparison would drift, and the one that drifted
        would be the one nobody was looking at."""
        from yfin.api.core import origin as shared

        assert origin_allowed is shared.origin_allowed


class TestConnectionLimits:
    """A WebSocket never passes through `RequestBrake` (a BaseHTTPMiddleware does not see
    this scope type) and the Origin check admits a client with no Origin, so without its
    own limit a loop of connections exhausts the threadpool and database pool `/v1` shares.
    """

    @pytest.mark.parametrize("overrides", [{"ui_ws_max_connections": 1}])
    def test_the_process_ceiling_closes_the_extra_socket(self, client: TestClient) -> None:
        with client.websocket_connect("/ui/ws") as first:
            assert first.receive_json()["op"] == Op.Live
            extra = client.websocket_connect("/ui/ws")
            with extra as second, pytest.raises(WebSocketDisconnect) as refused:
                second.receive_json()
            assert refused.value.code == WsClose.TooBusy

    @pytest.mark.parametrize("overrides", [{"ui_ws_max_connections": 1}])
    def test_a_closed_socket_gives_its_slot_back(self, client: TestClient) -> None:
        """The count is decremented in a `finally`: a page that reloads
        must not spend a slot permanently."""
        for _ in range(3):
            with client.websocket_connect("/ui/ws") as socket:
                assert socket.receive_json()["op"] == Op.Live

    @pytest.mark.parametrize("overrides", [{"ui_ws_connections_per_minute": 1}])
    def test_the_per_address_window_closes_the_second_handshake(
        self, client: TestClient
    ) -> None:
        with client.websocket_connect("/ui/ws") as socket:
            assert socket.receive_json()["op"] == Op.Live
        again = client.websocket_connect("/ui/ws")
        with again as socket, pytest.raises(WebSocketDisconnect) as refused:
            socket.receive_json()
        assert refused.value.code == WsClose.TooBusy


class _FakeSocket:
    """Records what the sender writes, and can stall inside one send."""

    def __init__(self, block_on: int | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()
        self._block_on = block_on

    async def send_json(self, frame: dict[str, Any]) -> None:
        self.sent.append(frame)
        if self._block_on is not None and len(self.sent) == self._block_on:
            self.blocked.set()
            await self.release.wait()


def _session(socket: _FakeSocket) -> LiveSession:
    return LiveSession(cast(WebSocket, socket), "test-request")


class _DisconnectingSocket(_FakeSocket):
    async def send_json(self, frame: dict[str, Any]) -> None:
        raise WebSocketDisconnect(code=1006)


async def _drain(session: LiveSession) -> None:
    while not session._out.empty():
        await asyncio.sleep(0)


class TestTheBoundedQueue:
    """Drop-oldest and the `dropped` frame are two of the three ordering
    decisions the module docstring names, and the spec gives them a row
    of their own. Nothing on the Python side executed either of them."""

    async def test_a_full_queue_drops_the_oldest_and_reports_the_count(self) -> None:
        socket = _FakeSocket()
        session = _session(socket)
        for index in range(QUEUE_MAXSIZE + 3):
            session._offer({"op": Op.Tick, "d": index})

        sender = asyncio.create_task(session._sender())
        try:
            await asyncio.wait_for(_drain(session), timeout=5)
        finally:
            sender.cancel()

        # The report comes FIRST: the page learns about the gap before
        # the frames that follow it, not after.
        assert socket.sent[0] == {"op": Op.Dropped, "n": 3}
        # Three oldest gone, newest kept: a page that has fallen behind
        # wants the current price, not the one from four seconds ago.
        assert [frame["d"] for frame in socket.sent[1:]] == list(
            range(3, QUEUE_MAXSIZE + 3)
        )

    async def test_a_drop_during_the_send_is_counted_into_the_NEXT_report(self) -> None:
        """The reset is read-and-reset with nothing awaited between the
        two halves. Move it after the send and these drops are lost --
        the page would then under-report a gap in its own tape."""
        socket = _FakeSocket(block_on=1)
        session = _session(socket)
        for index in range(QUEUE_MAXSIZE + 3):
            session._offer({"op": Op.Tick, "d": index})

        sender = asyncio.create_task(session._sender())
        try:
            await asyncio.wait_for(socket.blocked.wait(), timeout=5)
            assert socket.sent[0] == {"op": Op.Dropped, "n": 3}
            # The sender took one frame off the full queue, so the first
            # of these three fits and the other two displace the oldest:
            # two more drops, while the first report is still in flight.
            for index in range(QUEUE_MAXSIZE + 3, QUEUE_MAXSIZE + 6):
                session._offer({"op": Op.Tick, "d": index})
            socket.release.set()
            await asyncio.wait_for(_drain(session), timeout=5)
        finally:
            sender.cancel()

        # Counted into the second report rather than lost with the first.
        reports = [frame for frame in socket.sent if frame["op"] == Op.Dropped]
        assert reports == [{"op": Op.Dropped, "n": 3}, {"op": Op.Dropped, "n": 2}]


class TestSenderDisconnect:
    async def test_client_disconnect_does_not_escape_as_an_asgi_error(self) -> None:
        """A browser closing during a send is a normal socket lifecycle event."""
        session = _session(_DisconnectingSocket())
        session._offer({"op": Op.Live, "enabled": False})

        await session._sender()


class _DeadPubSub:
    """A pub/sub whose Redis went away after the socket opened."""

    def __init__(self) -> None:
        self.closed = False

    async def subscribe(self, *channels: str) -> None:
        raise ConnectionError("bus gone")

    async def unsubscribe(self, *channels: str) -> None:
        raise ConnectionError("bus gone")

    async def get_message(self, **kwargs: Any) -> dict[str, Any]:
        raise ConnectionError("bus gone")

    async def aclose(self) -> None:
        self.closed = True


def _live_session(socket: _FakeSocket) -> LiveSession:
    session = _session(socket)
    session._pubsub = _DeadPubSub()
    session._bus_ok = True
    return session


class TestRedisFailingMidSession:
    """Fail-open is the rule on every layer of the live path. The socket
    honoured it at open time only: a subscribe that raised killed the
    connection, and a reader that died left the page believing the feed
    was live while showing a price that had stopped moving."""

    async def test_a_failed_subscribe_turns_the_feed_off_instead_of_the_socket(
        self, stub_quotes: None
    ) -> None:
        socket = _FakeSocket()
        session = _live_session(socket)

        await session._subscribe(["AAPL"])

        assert session._bus_ok is False
        assert session._out.get_nowait() == {"op": Op.Live, "enabled": False}

    async def test_the_archive_still_answers_after_the_bus_dies(
        self, stub_quotes: None
    ) -> None:
        """The snapshot is what the page falls back to, so it has to
        survive the failure that made it necessary."""
        socket = _FakeSocket()
        session = _live_session(socket)

        await session._subscribe(["AAPL"])
        assert session._symbols == {"AAPL"}
        # `unsub` after the bus is gone is a no-op, not a second failure.
        await session._unsubscribe(["AAPL"])
        assert session._symbols == set()

    async def test_a_dead_reader_tells_the_page_before_it_returns(self) -> None:
        socket = _FakeSocket()
        session = _live_session(socket)
        session._symbols = {"AAPL"}

        await asyncio.wait_for(session._bus_reader(), timeout=5)

        assert session._bus_ok is False
        assert session._out.get_nowait() == {"op": Op.Live, "enabled": False}

    async def test_the_page_is_told_once_however_many_things_fail(self) -> None:
        """Two `enabled: false` frames would have the page flapping."""
        socket = _FakeSocket()
        session = _live_session(socket)
        session._symbols = {"AAPL"}

        await asyncio.wait_for(session._bus_reader(), timeout=5)
        await session._unsubscribe(["AAPL"])

        assert session._out.qsize() == 1
