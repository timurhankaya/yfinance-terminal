"""`/ui/ws`: the origin gate, the frame protocol and the fallback.

No Redis and no database here. What this file pins down is what the
socket does when the live bus is NOT available -- which is the state
every deployment starts in, since `yf_stream_publish_enabled` defaults to
off -- and the rules that hold whether it is available or not: who may
open a socket, what a bad frame does, and the per-connection ceiling.

The pub/sub half is covered against fakeredis in tests/repo.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from yfin.api.core.config import ApiSettings
from yfin.ui.live import MAX_SYMBOLS, Op, WsClose, WsError, origin_allowed

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


@pytest.fixture
def quotes() -> list[dict[str, Any]]:
    """What the stubbed `live_quotes` read returns."""
    return []


@pytest.fixture
def client(
    tmp_path: Path,
    quotes: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    from yfin.api.app import create_app
    from yfin.ui import live, pages

    monkeypatch.setattr(pages, "default_dist_dir", lambda: tmp_path / "absent")

    async def stub_quotes(self: Any, symbols: list[str]) -> list[dict[str, Any]]:
        # Stubbed at the session, not at `data.list_quotes`: the real one
        # would build an engine and dial PostgreSQL, and what is under
        # test here is the frame order, not the query.
        return [body for body in quotes if body["s"] in symbols]

    monkeypatch.setattr(live.LiveSession, "_quotes", stub_quotes)
    with TestClient(create_app(settings())) as test_client:
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

    def test_too_many_symbols_refuses_the_whole_frame(self, client: TestClient) -> None:
        """Half a watchlist with nothing saying which half is worse than
        none of it."""
        with client.websocket_connect("/ui/ws") as socket:
            socket.receive_json()
            symbols = [f"S{index}" for index in range(MAX_SYMBOLS + 1)]
            socket.send_json({"op": Op.Sub, "symbols": symbols})
            assert socket.receive_json() == {"op": Op.Error, "code": WsError.TooMany}

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
    def test_the_close_code_is_in_the_private_range(self) -> None:
        """4000-4999 is what a browser hands back to the page; anything
        below 4000 is reserved and some browsers rewrite it."""
        assert 4000 <= WsClose.BadOrigin <= 4999
