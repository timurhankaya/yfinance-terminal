"""`/ui/api/screens`: the four-table join the public surface will not do.

What is pinned here is what the generic dataset surface cannot express:
the latest run per screen in one query, the roster in the SCREEN's order
rather than by symbol, and an outer join that keeps a member whose quote
row is missing. Each of those is a place where a plausible
implementation quietly returns something that reads as data.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, insert
from sqlalchemy.orm import Session, sessionmaker

from yfin.api.auth import dependencies as auth_deps
from yfin.api.core.config import ApiSettings
from yfin.api.ratelimit import concurrency, limiter, usage
from yfin.api.storage import session as api_session
from yfin.models import Symbol
from yfin.models.discovery import Screen, ScreenMember, ScreenRun, screen_quotes
from yfin.ui import pages

pytestmark = pytest.mark.repo

SIGNING_KEY = "k" * 48
TODAY = date(2026, 9, 8)
YESTERDAY = date(2026, 9, 7)
FETCHED = datetime(2026, 9, 8, 20, 5, tzinfo=UTC)


def api_settings() -> ApiSettings:
    return ApiSettings(
        jwt_signing_key=SIGNING_KEY,
        jwt_kid="k1",
        jwt_issuer="yfin-api",
        jwt_audience="yfin-api",
        ui_enabled=True,
    )


def _run(key: str, day: date, **over: Any) -> dict[str, Any]:
    return {
        "screen_key": key,
        "as_of_date": day,
        "total": 120,
        "fetched_rows": 100,
        "row_count": 100,
        "page_count": 1,
        "content_hash": "h" * 16,
        "fetched_at": FETCHED,
        **over,
    }


def _quote(symbol: str, day: date, **over: Any) -> dict[str, Any]:
    """One quote row with a UNIFORM key set.

    `executemany` compiles one statement for the whole list, so a row
    that omits a column its neighbour has is a StatementError rather
    than a NULL.
    """
    row: dict[str, Any] = {
        "symbol": symbol,
        "as_of_date": day,
        "short_name": None,
        "currency": None,
        "exchange": None,
        "market_state": None,
        "regular_market_price": None,
        "regular_market_change": None,
        "regular_market_change_percent": None,
        "regular_market_volume": None,
        "market_cap": None,
        "is_known": True,
        "fetched_at": FETCHED,
        "raw_json": "{}",
    }
    row.update(over)
    return row


def _member(key: str, day: date, symbol: str, rank: int, known: bool = True) -> dict[str, Any]:
    return {
        "screen_key": key,
        "as_of_date": day,
        "symbol": symbol,
        "rank_index": rank,
        "is_known": known,
        "fetched_at": FETCHED,
    }


@pytest.fixture
def redis() -> Iterator[fakeredis.FakeRedis]:
    server = fakeredis.FakeRedis(decode_responses=True)
    yield server
    server.flushall()


@pytest.fixture
def seeded(test_engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
    with factory() as session_:
        session_.execute(
            insert(Symbol),
            [
                {"symbol": "AAA", "exchange": "NMS", "is_active": True},
                {"symbol": "BBB", "exchange": "NMS", "is_active": True},
                {"symbol": "CCC", "exchange": "NMS", "is_active": True},
            ],
        )
        session_.execute(
            insert(Screen),
            [
                {
                    "screen_key": "day_gainers",
                    "kind": "predefined",
                    "quote_type": "EQUITY",
                    "title": "Day gainers",
                    "description": "Up the most today.",
                    "sort_field": "percentchange",
                    "sort_asc": False,
                    "is_enabled": True,
                    "created_at": FETCHED,
                    "updated_at": FETCHED,
                },
                {
                    # Enabled, never run: it has to appear, or a
                    # misconfigured screen hides behind an absence.
                    "screen_key": "never_ran",
                    "kind": "predefined",
                    "quote_type": "EQUITY",
                    "title": "Aaa never ran",
                    "sort_field": "percentchange",
                    "sort_asc": False,
                    "is_enabled": True,
                    "created_at": FETCHED,
                    "updated_at": FETCHED,
                },
                {
                    # Switched off by an operator: its roster goes stale
                    # from that day on, so the terminal must not offer it.
                    "screen_key": "switched_off",
                    "kind": "predefined",
                    "quote_type": "ETF",
                    "title": "Zzz switched off",
                    "sort_field": "percentchange",
                    "sort_asc": False,
                    "is_enabled": False,
                    "created_at": FETCHED,
                    "updated_at": FETCHED,
                },
            ],
        )
        session_.execute(
            insert(ScreenRun),
            [
                _run("day_gainers", YESTERDAY, total=9, row_count=1),
                _run("day_gainers", TODAY),
                _run("switched_off", TODAY),
            ],
        )
        session_.execute(
            insert(ScreenMember),
            [
                # Deliberately not in symbol order: rank 0 is CCC.
                _member("day_gainers", TODAY, "CCC", 0),
                _member("day_gainers", TODAY, "AAA", 1),
                _member("day_gainers", TODAY, "BBB", 2, known=False),
                _member("day_gainers", YESTERDAY, "AAA", 0),
            ],
        )
        session_.execute(
            insert(screen_quotes),
            [
                _quote(
                    "CCC",
                    TODAY,
                    short_name="Cee Corp",
                    currency="USD",
                    exchange="NMS",
                    market_state="REGULAR",
                    regular_market_price=Decimal("12.50"),
                    regular_market_change=Decimal("2.50"),
                    regular_market_change_percent=Decimal("25"),
                    regular_market_volume=4_000_000,
                    market_cap=Decimal("1250000000"),
                ),
                _quote("AAA", TODAY, short_name="Aaa Inc", regular_market_price=Decimal("3.10")),
                # Yesterday's quote for a symbol in today's roster: the
                # join is on (symbol, as_of_date), so this must not leak
                # into today's grid.
                _quote("BBB", YESTERDAY, short_name="Bbb Ltd", regular_market_price=Decimal("99")),
            ],
        )
        session_.commit()
        yield session_

        session_.execute(screen_quotes.delete())
        for model in (ScreenMember, ScreenRun, Screen, Symbol):
            session_.query(model).delete()
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


class TestList:
    def test_only_enabled_screens_are_offered(self, client: TestClient) -> None:
        body = client.get("/ui/api/screens").json()
        keys = [row["screen_key"] for row in body["data"]]
        assert keys == ["never_ran", "day_gainers"]  # by title: "Aaa…", "Day gainers"

    def test_each_screen_carries_its_latest_run(self, client: TestClient) -> None:
        """`DISTINCT ON`, so yesterday's run must not win."""
        body = client.get("/ui/api/screens").json()
        gainers = next(row for row in body["data"] if row["screen_key"] == "day_gainers")
        assert gainers["as_of_date"] == "2026-09-08"
        assert gainers["total"] == 120
        assert gainers["row_count"] == 100

    def test_a_screen_that_never_ran_is_listed_with_nulls(self, client: TestClient) -> None:
        body = client.get("/ui/api/screens").json()
        never = next(row for row in body["data"] if row["screen_key"] == "never_ran")
        assert never["as_of_date"] is None
        assert never["total"] is None


class TestDetail:
    def test_the_roster_is_in_the_screen_s_own_order(self, client: TestClient) -> None:
        """Sorting by symbol would throw away the one thing a screener's
        roster carries beyond a list of tickers."""
        body = client.get("/ui/api/screens/day_gainers").json()
        assert [row["symbol"] for row in body["data"]["rows"]] == ["CCC", "AAA", "BBB"]
        assert [row["rank_index"] for row in body["data"]["rows"]] == [0, 1, 2]

    def test_the_quote_columns_come_from_the_same_day(self, client: TestClient) -> None:
        rows = client.get("/ui/api/screens/day_gainers").json()["data"]["rows"]
        top = rows[0]
        assert top["short_name"] == "Cee Corp"
        assert top["price"] == "12.500000000000"
        assert top["volume"] == 4_000_000
        assert top["market_state"] == "REGULAR"

    def test_a_member_with_no_quote_for_that_day_is_kept(self, client: TestClient) -> None:
        """`screen_quotes` is outside the gate's delete scope, so a member
        can exist without one. Dropping it would quietly shorten a roster
        whose length is itself reported."""
        rows = client.get("/ui/api/screens/day_gainers").json()["data"]["rows"]
        bbb = next(row for row in rows if row["symbol"] == "BBB")
        assert bbb["price"] is None
        # Yesterday's BBB quote exists and must not have leaked in.
        assert bbb["short_name"] is None
        assert bbb["is_known"] is False

    def test_only_the_latest_run_s_roster(self, client: TestClient) -> None:
        rows = client.get("/ui/api/screens/day_gainers").json()["data"]["rows"]
        assert len(rows) == 3  # yesterday's single member is not mixed in

    def test_the_envelope_says_when_it_was_fetched(self, client: TestClient) -> None:
        """A screen run IS a fetch, unlike the other UI routes, so `as_of`
        is a real answer rather than a null."""
        body = client.get("/ui/api/screens/day_gainers").json()
        assert body["as_of"].startswith("2026-09-08T20:05")

    def test_the_limit_marks_that_there_is_more(self, client: TestClient) -> None:
        body = client.get("/ui/api/screens/day_gainers", params={"limit": 2}).json()
        assert len(body["data"]["rows"]) == 2
        assert body["data"]["truncated"] is True
        assert body["data"]["offset"] == 0

    def test_a_full_roster_is_not_marked_cut(self, client: TestClient) -> None:
        body = client.get("/ui/api/screens/day_gainers", params={"limit": 3}).json()
        assert body["data"]["truncated"] is False

    def test_offset_walks_the_roster_in_rank_order(self, client: TestClient) -> None:
        """A roster is 1,000 rows at the default `yf_screen_size` x
        `yf_screen_max_pages`, so the first page is not the list."""
        body = client.get(
            "/ui/api/screens/day_gainers", params={"limit": 2, "offset": 2}
        ).json()
        assert [row["symbol"] for row in body["data"]["rows"]] == ["BBB"]
        assert body["data"]["offset"] == 2
        assert body["data"]["truncated"] is False

    def test_an_offset_past_the_end_is_empty_rather_than_an_error(
        self, client: TestClient
    ) -> None:
        body = client.get("/ui/api/screens/day_gainers", params={"offset": 500}).json()
        assert body["data"]["rows"] == []
        assert body["data"]["truncated"] is False

    def test_a_screen_that_never_ran_has_an_empty_roster_not_a_404(
        self, client: TestClient
    ) -> None:
        """"No such screen" and "nothing has fetched it yet" are different
        problems, and the panel says different things about them."""
        body = client.get("/ui/api/screens/never_ran")
        assert body.status_code == 200
        assert body.json()["data"]["rows"] == []

    def test_an_unknown_screen_is_a_404_problem(self, client: TestClient) -> None:
        response = client.get("/ui/api/screens/no_such_screen")
        assert response.status_code == 404
        assert response.json()["type"] == "not_found"

    def test_a_disabled_screen_is_still_readable_by_key(self, client: TestClient) -> None:
        """It is left off the LIST because its roster goes stale, but a
        URL someone kept must not 404 -- the data is still there and the
        run date on it says how old it is."""
        assert client.get("/ui/api/screens/switched_off").status_code == 200
