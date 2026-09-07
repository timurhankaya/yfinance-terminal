"""The live path end to end: a real schema and a real pub/sub.

`tests/unit/test_ui_live.py` covers the socket with the bus down, which
is the default state. This covers the one that matters in production: a
tick published after a commit reaches an open page, in the right order,
without the snapshot racing it.

The bus is fakeredis rather than a mock. What is under test is the
subscribe-then-snapshot ordering and the channel naming, and a mock that
answers whatever it is asked would confirm both no matter what the code
did.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import fakeredis
import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, insert
from sqlalchemy.orm import Session, sessionmaker

from yfin.api.auth import dependencies as auth_deps
from yfin.api.core.config import ApiSettings
from yfin.api.ratelimit import concurrency, limiter, usage
from yfin.api.storage import session as api_session
from yfin.core.config import get_settings
from yfin.models import Symbol
from yfin.models.bars import GAP_FETCH_FAILED, GAP_RETENTION_EXPIRED, BarGap
from yfin.models.stream import LiveQuote, LiveTick
from yfin.stream.publish import channel
from yfin.ui import live, pages

pytestmark = pytest.mark.repo

SIGNING_KEY = "k" * 48
SYMBOL = "AAPL"
BASE = datetime(2026, 9, 8, 14, 30, tzinfo=UTC)
BUS_URL = "redis://fake/0"


def api_settings() -> ApiSettings:
    return ApiSettings(
        jwt_signing_key=SIGNING_KEY,
        jwt_kid="k1",
        jwt_issuer="yfin-api",
        jwt_audience="yfin-api",
        ui_enabled=True,
    )


def _tick(offset_ms: int, price: str, digest: str) -> dict[str, Any]:
    return {
        "symbol": SYMBOL,
        "ts_utc": BASE + timedelta(milliseconds=offset_ms),
        "payload_hash": digest,
        "received_at": BASE,
        "quote_type_code": 8,
        "market_hours_code": 1,
        "price": Decimal(price),
    }


@pytest.fixture
def bus() -> Iterator[fakeredis.FakeServer]:
    """One server, two clients: the publisher is synchronous like the
    writer's, the subscriber is async like the socket's."""
    server = fakeredis.FakeServer()
    yield server


@pytest.fixture
def redis() -> Iterator[fakeredis.FakeRedis]:
    server = fakeredis.FakeRedis(decode_responses=True)
    yield server
    server.flushall()


@pytest.fixture
def seeded(test_engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
    with factory() as session_:
        # `live_ticks.symbol` and `bar_gaps.symbol` both carry a foreign
        # key; without the parent row the whole insert aborts.
        session_.execute(
            insert(Symbol),
            [
                {"symbol": SYMBOL, "exchange": "NMS", "quote_type": "EQUITY", "is_active": True},
                {"symbol": "MSFT", "exchange": "NMS", "quote_type": "EQUITY", "is_active": True},
            ],
        )
        session_.execute(
            insert(LiveTick),
            [
                _tick(0, "232.10", "a" * 16),
                _tick(1000, "232.35", "b" * 16),
                _tick(2000, "232.50", "c" * 16),
            ],
        )
        session_.execute(
            insert(LiveQuote),
            [
                {
                    "symbol": SYMBOL,
                    "ts_utc": BASE + timedelta(milliseconds=2000),
                    "payload_hash": "c" * 16,
                    "updated_at": BASE,
                    "received_at": BASE,
                    "quote_type_code": 8,
                    "market_hours_code": 1,
                    "price": Decimal("232.50"),
                },
                {
                    # A second quote so a `sub` for MSFT always answers.
                    # Every `receive_json` below has to be preceded by
                    # something that is guaranteed to produce a frame --
                    # the test client's receive has no timeout, so a
                    # missing frame is a hung suite rather than a failure.
                    "symbol": "MSFT",
                    "ts_utc": BASE,
                    "payload_hash": "d" * 16,
                    "updated_at": BASE,
                    "received_at": BASE,
                    "quote_type_code": 8,
                    "market_hours_code": 1,
                    "price": Decimal("501.10"),
                },
            ],
        )
        session_.execute(
            insert(BarGap),
            [
                {
                    "symbol": SYMBOL,
                    "bar_interval": "5m",
                    "gap_start_utc": BASE,
                    "gap_end_utc": BASE + timedelta(minutes=30),
                    "detected_at": BASE,
                    "reason": GAP_FETCH_FAILED,
                    "resolved_at": None,
                },
                {
                    # Closed: the bars are in the archive now, so shading
                    # this window would be a lie.
                    "symbol": SYMBOL,
                    "bar_interval": "5m",
                    "gap_start_utc": BASE + timedelta(hours=1),
                    "gap_end_utc": BASE + timedelta(hours=2),
                    "detected_at": BASE,
                    "reason": GAP_RETENTION_EXPIRED,
                    "resolved_at": BASE + timedelta(hours=3),
                },
                {
                    # Another interval's gap; the 5m chart must not show it.
                    "symbol": SYMBOL,
                    "bar_interval": "1m",
                    "gap_start_utc": BASE,
                    "gap_end_utc": BASE + timedelta(minutes=10),
                    "detected_at": BASE,
                    "reason": GAP_FETCH_FAILED,
                    "resolved_at": None,
                },
            ],
        )
        session_.commit()
        yield session_

        for model in (BarGap, LiveQuote, LiveTick, Symbol):
            session_.query(model).delete()
        session_.commit()


@pytest.fixture
def client(
    seeded: Session,
    test_engine: Engine,
    redis: fakeredis.FakeRedis,
    bus: fakeredis.FakeServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    from yfin.api.app import create_app

    factory = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(api_session, "get_session_factory", lambda: factory)
    for module in (limiter, concurrency, usage, auth_deps):
        monkeypatch.setattr(module, "get_redis", lambda _s: redis)
    monkeypatch.setattr(pages, "default_dist_dir", lambda: tmp_path / "absent")

    # The two settings that turn the live path on, without touching the
    # settings table: the socket reads them through `get_settings`.
    core = get_settings().model_copy(
        update={
            "yf_stream_publish_enabled": True,
            "yf_stream_publish_redis_url": BUS_URL,
        }
    )
    monkeypatch.setattr(live, "get_settings", lambda: core)
    monkeypatch.setattr(
        live,
        "connect_bus",
        lambda _url: fakeredis.aioredis.FakeRedis(server=bus, decode_responses=True),
    )

    def scope() -> Iterator[Session]:
        with factory() as request_session:
            yield request_session

    app = create_app(api_settings())
    app.dependency_overrides[api_session.session_scope] = scope
    with TestClient(app) as test_client:
        yield test_client


class TestTicks:
    def test_the_newest_ticks_come_first(self, client: TestClient) -> None:
        """`QR` is a time-and-sales list: the top row is the last trade."""
        body = client.get(f"/ui/api/symbols/{SYMBOL}/ticks").json()
        assert [row["p"] for row in body["data"]] == ["232.5", "232.35", "232.1"]

    def test_a_tick_row_is_the_same_shape_the_socket_sends(self, client: TestClient) -> None:
        """One row shape, so the opening page and everything that arrives
        afterwards render through the same code."""
        row = client.get(f"/ui/api/symbols/{SYMBOL}/ticks", params={"limit": 1}).json()
        assert row["data"][0] == {
            "s": SYMBOL,
            "t": int((BASE + timedelta(milliseconds=2000)).timestamp() * 1000),
            "p": "232.5",
            "mh": 1,
        }

    def test_the_limit_caps_the_list(self, client: TestClient) -> None:
        body = client.get(f"/ui/api/symbols/{SYMBOL}/ticks", params={"limit": 2}).json()
        assert len(body["data"]) == 2

    def test_another_symbol_is_empty_rather_than_wrong(self, client: TestClient) -> None:
        assert client.get("/ui/api/symbols/MSFT/ticks").json()["data"] == []


class TestGaps:
    def test_only_open_gaps_are_returned(self, client: TestClient) -> None:
        body = client.get(f"/ui/api/symbols/{SYMBOL}/gaps", params={"interval": "5m"}).json()
        starts = [row["gap_start_utc"] for row in body["data"]]
        assert len(starts) == 1
        assert starts[0].startswith("2026-09-08T14:30")

    def test_the_interval_selects_the_table_the_chart_is_drawing(
        self, client: TestClient
    ) -> None:
        body = client.get(f"/ui/api/symbols/{SYMBOL}/gaps", params={"interval": "1m"}).json()
        assert [row["bar_interval"] for row in body["data"]] == ["1m"]

    def test_a_window_keeps_a_gap_that_runs_into_it(self, client: TestClient) -> None:
        """Overlap, not containment: a gap that started before the chart's
        first candle and ends inside it is exactly the one worth shading."""
        start = (BASE + timedelta(minutes=15)).isoformat()
        body = client.get(
            f"/ui/api/symbols/{SYMBOL}/gaps", params={"interval": "5m", "from": start}
        ).json()
        assert len(body["data"]) == 1

    def test_a_window_after_the_gap_drops_it(self, client: TestClient) -> None:
        start = (BASE + timedelta(hours=6)).isoformat()
        body = client.get(
            f"/ui/api/symbols/{SYMBOL}/gaps", params={"interval": "5m", "from": start}
        ).json()
        assert body["data"] == []


class TestSocket:
    def test_the_page_is_told_the_bus_is_up(self, client: TestClient) -> None:
        with client.websocket_connect("/ui/ws") as socket:
            assert socket.receive_json() == {"op": "live", "enabled": True}

    def test_sub_snapshots_from_live_quotes(self, client: TestClient) -> None:
        with client.websocket_connect("/ui/ws") as socket:
            socket.receive_json()
            socket.send_json({"op": "sub", "symbols": [SYMBOL]})
            frame = socket.receive_json()
        assert frame["op"] == "snap"
        assert frame["d"]["p"] == "232.5"

    def test_a_published_tick_reaches_the_page(
        self, client: TestClient, bus: fakeredis.FakeServer
    ) -> None:
        """The whole point of 1c, in one assertion: what the writer
        published after its commit is what the open page receives."""
        publisher = fakeredis.FakeRedis(server=bus, decode_responses=True)
        body = {"s": SYMBOL, "t": 1788877815250, "p": "233.00", "mh": 1}
        with client.websocket_connect("/ui/ws") as socket:
            socket.receive_json()
            socket.send_json({"op": "sub", "symbols": [SYMBOL]})
            assert socket.receive_json()["op"] == "snap"
            publisher.publish(channel(SYMBOL), json.dumps(body))
            assert socket.receive_json() == {"op": "tick", "d": body}

    def test_a_tick_for_a_symbol_nobody_asked_for_is_not_delivered(
        self, client: TestClient, bus: fakeredis.FakeServer
    ) -> None:
        """One channel per symbol is what keeps a page watching two
        symbols off the other nine thousand."""
        publisher = fakeredis.FakeRedis(server=bus, decode_responses=True)
        mine = {"s": SYMBOL, "t": 1788877815250, "p": "233.00", "mh": 1}
        with client.websocket_connect("/ui/ws") as socket:
            socket.receive_json()
            socket.send_json({"op": "sub", "symbols": [SYMBOL]})
            assert socket.receive_json()["op"] == "snap"
            publisher.publish(channel("MSFT"), json.dumps({"s": "MSFT", "t": 1, "p": "1", "mh": 1}))
            publisher.publish(channel(SYMBOL), json.dumps(mine))
            assert socket.receive_json() == {"op": "tick", "d": mine}

    def test_unsub_stops_the_ticks(
        self, client: TestClient, bus: fakeredis.FakeServer
    ) -> None:
        publisher = fakeredis.FakeRedis(server=bus, decode_responses=True)
        with client.websocket_connect("/ui/ws") as socket:
            socket.receive_json()
            socket.send_json({"op": "sub", "symbols": [SYMBOL]})
            assert socket.receive_json()["op"] == "snap"
            socket.send_json({"op": "unsub", "symbols": [SYMBOL]})
            socket.send_json({"op": "sub", "symbols": ["MSFT"]})
            # The MSFT snap is the fence: frames are handled in order, so
            # by the time it arrives the unsub has been applied.
            assert socket.receive_json()["d"]["s"] == "MSFT"
            publisher.publish(
                channel(SYMBOL), json.dumps({"s": SYMBOL, "t": 1, "p": "1", "mh": 1})
            )
            publisher.publish(
                channel("MSFT"), json.dumps({"s": "MSFT", "t": 2, "p": "2", "mh": 1})
            )
            assert socket.receive_json()["d"]["s"] == "MSFT"
