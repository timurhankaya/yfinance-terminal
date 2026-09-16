"""`GET /ui/api/sparklines` against a real schema.

The window is counted in SESSIONS rather than calendar days, the closes come
back in date order, and a symbol with no bars is named in `missing`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, insert
from sqlalchemy.orm import Session, sessionmaker

from yfin.api.auth import dependencies as auth_deps
from yfin.api.core.config import ApiSettings
from yfin.api.ratelimit import concurrency, limiter, usage
from yfin.api.storage import session as api_session
from yfin.models.prices import PriceHistory
from yfin.models.symbols import Symbol
from yfin.ui import pages

pytestmark = pytest.mark.repo

SIGNING_KEY = "k" * 48

#: A Monday, so the weekend the fixture skips is unambiguous.
FIRST_SESSION = date(2026, 1, 5)
SESSIONS = 40


def api_settings() -> ApiSettings:
    return ApiSettings(
        jwt_signing_key=SIGNING_KEY,
        jwt_kid="k1",
        jwt_issuer="yfin-api",
        jwt_audience="yfin-api",
        ui_enabled=True,
    )


@pytest.fixture
def redis() -> Iterator[fakeredis.FakeRedis]:
    server = fakeredis.FakeRedis(decode_responses=True)
    yield server
    server.flushall()


def _sessions(count: int) -> list[date]:
    """Weekdays only, which is what makes points-not-days observable."""
    days: list[date] = []
    day = FIRST_SESSION
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


@pytest.fixture
def seeded(test_engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
    days = _sessions(SESSIONS)
    with factory() as session_:
        session_.execute(
            insert(Symbol),
            [{"symbol": "AAPL"}, {"symbol": "MSFT"}, {"symbol": "QUIET"}],
        )
        session_.execute(
            insert(PriceHistory),
            [
                {
                    "symbol": symbol,
                    "session_date": day,
                    "ts_utc": datetime(day.year, day.month, day.day, tzinfo=UTC),
                    "close": Decimal(base + index),
                }
                for symbol, base in (("AAPL", 100), ("MSFT", 300))
                for index, day in enumerate(days)
            ],
        )
        session_.commit()
        yield session_

        session_.query(PriceHistory).delete()
        session_.query(Symbol).delete()
        session_.commit()


@pytest.fixture
def client(
    seeded: Session,
    test_engine: Engine,
    redis: fakeredis.FakeRedis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    from yfin.api.app import create_app

    factory = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(api_session, "get_session_factory", lambda: factory)
    for module in (limiter, concurrency, usage, auth_deps):
        monkeypatch.setattr(module, "get_redis", lambda _s: redis)
    monkeypatch.setattr(pages, "default_dist_dir", lambda: tmp_path / "absent")

    def scope() -> Iterator[Session]:
        with factory() as request_session:
            yield request_session

    app = create_app(api_settings())
    app.dependency_overrides[api_session.session_scope] = scope
    with TestClient(app) as test_client:
        yield test_client


def test_the_window_is_sessions_not_calendar_days(client: TestClient) -> None:
    """Ten points is ten closes, over fourteen calendar days of weekdays."""
    body = client.get("/ui/api/sparklines", params={"symbols": "AAPL", "points": 10}).json()
    series = body["data"]["series"][0]
    assert len(series["closes"]) == 10
    days = _sessions(SESSIONS)
    assert series["first_date"] == days[-10].isoformat()
    assert series["last_date"] == days[-1].isoformat()


def test_the_closes_are_the_newest_ones_in_date_order(client: TestClient) -> None:
    body = client.get("/ui/api/sparklines", params={"symbols": "AAPL", "points": 5}).json()
    # The fixture's close rises by one per session, so the tail is the
    # last five and the order is visible in the values themselves.
    assert body["data"]["series"][0]["closes"] == [
        "135.000000000000",
        "136.000000000000",
        "137.000000000000",
        "138.000000000000",
        "139.000000000000",
    ]


def test_several_symbols_come_back_in_the_order_asked_for(client: TestClient) -> None:
    body = client.get(
        "/ui/api/sparklines", params={"symbols": "msft,aapl", "points": 5}
    ).json()
    assert [row["symbol"] for row in body["data"]["series"]] == ["MSFT", "AAPL"]
    assert body["data"]["missing"] == []


def test_a_symbol_with_no_bars_is_named_rather_than_dropped(client: TestClient) -> None:
    body = client.get(
        "/ui/api/sparklines", params={"symbols": "AAPL,QUIET,ZZZZ"}
    ).json()
    assert [row["symbol"] for row in body["data"]["series"]] == ["AAPL"]
    assert body["data"]["missing"] == ["QUIET", "ZZZZ"]


def test_a_window_longer_than_the_archive_returns_what_there_is(client: TestClient) -> None:
    body = client.get("/ui/api/sparklines", params={"symbols": "AAPL", "points": 90}).json()
    # 90 sessions asked for, 40 in the archive, and the calendar window
    # (180 days) covers all of them.
    assert len(body["data"]["series"][0]["closes"]) == SESSIONS


def test_the_envelope_is_a_resource(client: TestClient) -> None:
    body = client.get("/ui/api/sparklines", params={"symbols": "AAPL"}).json()
    assert set(body) == {"data", "as_of"}
    assert body["as_of"] is None
    assert body["data"]["points"] == 30
