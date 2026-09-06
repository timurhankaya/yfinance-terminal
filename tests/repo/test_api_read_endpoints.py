"""The core read endpoints, end to end against a real schema.

These run through the HTTP layer on purpose. The parts most likely to
break are the seams -- interval to table, session filter, cursor to sort
key, Decimal to JSON -- and none of them are visible from a repository
test alone.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, insert
from sqlalchemy.orm import Session, sessionmaker

from yfin.api.app import create_app
from yfin.api.auth import dependencies as auth_deps
from yfin.api.auth import jwt as tokens
from yfin.api.core.config import ApiSettings
from yfin.api.ratelimit import concurrency, limiter, policy, usage
from yfin.api.ratelimit.policy import PlanLimits
from yfin.api.storage import session as api_session
from yfin.models import PeriodicBar, PriceBar, PriceHistory, Symbol
from yfin.models.prices import Dividend, Split

pytestmark = pytest.mark.repo

SIGNING_KEY = "k" * 48
CLIENT_ID = "yfc_" + "r" * 32
SYMBOL = "TESTCO"
ALL_SCOPES = (
    "reference:read",
    "bars:read",
    "fundamentals:read",
)

LIMITS = PlanLimits(
    plan="test",
    requests_per_second=1000,
    burst=1000,
    monthly_quota=1_000_000,
    max_page_size=500,
    max_concurrency=50,
)


def api_settings() -> ApiSettings:
    return ApiSettings(
        jwt_signing_key=SIGNING_KEY,
        jwt_kid="k1",
        jwt_issuer="yfin-api",
        jwt_audience="yfin-api",
    )


@pytest.fixture
def redis() -> Iterator[fakeredis.FakeRedis]:
    server = fakeredis.FakeRedis(decode_responses=True)
    yield server
    server.flushall()


@pytest.fixture
def seeded(test_engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
    base = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    with factory() as session:
        session.execute(
            insert(Symbol),
            [
                {
                    "symbol": SYMBOL,
                    "exchange": "NMS",
                    "quote_type": "EQUITY",
                    "currency": "USD",
                    "is_active": True,
                },
                {"symbol": "TESTZZ", "exchange": "NYQ", "is_active": True},
                {"symbol": "TESTOFF", "exchange": "NMS", "is_active": False},
            ],
        )
        session.execute(
            insert(PriceHistory),
            [
                {
                    "symbol": SYMBOL,
                    "session_date": date(2026, 1, 5) + timedelta(days=offset),
                    "ts_utc": base + timedelta(days=offset),
                    "open": Decimal("10.5"),
                    "high": Decimal("11"),
                    "low": Decimal("10"),
                    "close": Decimal("10.75"),
                    "adj_close": Decimal("10.70"),
                    "volume": 1000 + offset,
                }
                for offset in range(5)
            ],
        )
        session.execute(
            insert(PriceBar),
            [
                {
                    "symbol": SYMBOL,
                    "bar_interval": "1m",
                    "ts_utc": base + timedelta(minutes=offset),
                    "local_date": date(2026, 1, 5),
                    "open": Decimal("10"),
                    "high": Decimal("10"),
                    "low": Decimal("10"),
                    "close": Decimal("10"),
                    "volume": 5,
                    "is_extended": offset >= 3,
                }
                for offset in range(5)
            ],
        )
        session.execute(
            insert(PeriodicBar),
            [
                {
                    "symbol": SYMBOL,
                    "bar_interval": "1wk",
                    "ts_utc": base + timedelta(weeks=offset),
                    "local_date": date(2026, 1, 5) + timedelta(weeks=offset),
                    "open": Decimal("10"),
                    "high": Decimal("10"),
                    "low": Decimal("10"),
                    "close": Decimal("10"),
                    "volume": 7,
                }
                for offset in range(3)
            ],
        )
        session.execute(
            insert(Dividend),
            [{"symbol": SYMBOL, "ex_date": date(2026, 1, 6), "amount": Decimal("0.25")}],
        )
        session.execute(
            insert(Split),
            [{"symbol": SYMBOL, "split_date": date(2026, 1, 7), "ratio": Decimal("2")}],
        )
        session.commit()
        yield session

        # The schema outlives a single test, so the fixture owns cleanup.
        # Children before parents: price rows reference symbols.
        for model in (Dividend, Split, PriceBar, PeriodicBar, PriceHistory, Symbol):
            session.query(model).delete()
        session.commit()


@pytest.fixture
def client(
    seeded: Session,
    test_engine: Engine,
    redis: fakeredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    factory = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(api_session, "get_session_factory", lambda: factory)
    for module in (limiter, concurrency, usage, auth_deps):
        monkeypatch.setattr(module, "get_redis", lambda _s: redis)
    monkeypatch.setattr(policy, "limits_for_client", lambda _cid: LIMITS)

    def scope() -> Iterator[Session]:
        with factory() as request_session:
            yield request_session

    app = create_app(api_settings())
    # The test schema lives in a process-specific search_path, so requests
    # have to use the fixture's engine rather than the process-wide one.
    app.dependency_overrides[api_session.session_scope] = scope
    with TestClient(app) as test_client:
        yield test_client


def _auth() -> dict[str, str]:
    token, _ = tokens.mint(
        api_settings(), client_id=CLIENT_ID, scopes=ALL_SCOPES, secret_id=1, epoch=0
    )
    return {"Authorization": f"Bearer {token}"}


def _get(client: TestClient, path: str, **params: Any) -> Any:
    return client.get(path, params=params, headers=_auth())


# --- symbols ----------------------------------------------------------------


def test_symbols_are_listed_with_the_envelope(client: TestClient) -> None:
    body = _get(client, "/v1/symbols").json()
    assert "data" in body and "next_cursor" in body
    assert {row["symbol"] for row in body["data"]} >= {SYMBOL, "TESTZZ"}


def test_inactive_symbols_are_hidden_by_default(client: TestClient) -> None:
    """An inactive row is one discovery found but nobody activated, so the
    pipeline never fetched it and it is close to empty."""
    codes = {row["symbol"] for row in _get(client, "/v1/symbols").json()["data"]}
    assert "TESTOFF" not in codes
    codes = {
        row["symbol"] for row in _get(client, "/v1/symbols", active=False).json()["data"]
    }
    assert "TESTOFF" in codes


def test_the_prefix_filter_matches_the_symbol(client: TestClient) -> None:
    body = _get(client, "/v1/symbols", q="testz").json()
    assert [row["symbol"] for row in body["data"]] == ["TESTZZ"]


def test_a_wildcard_in_the_prefix_is_taken_literally(client: TestClient) -> None:
    """Otherwise a bare `%` turns a prefix lookup into a full scan."""
    assert _get(client, "/v1/symbols", q="%%").json()["data"] == []


def test_paging_walks_the_whole_list_without_repeats(client: TestClient) -> None:
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        params = {"limit": 1}
        if cursor:
            params["cursor"] = cursor
        body = _get(client, "/v1/symbols", **params).json()
        seen.extend(row["symbol"] for row in body["data"])
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen))
    assert {SYMBOL, "TESTZZ"} <= set(seen)


def test_a_cursor_from_another_query_is_422(client: TestClient) -> None:
    cursor = _get(client, "/v1/symbols", limit=1).json()["next_cursor"]
    response = _get(client, "/v1/symbols", limit=1, exchange="NMS", cursor=cursor)
    assert response.status_code == 422
    assert response.json()["type"] == "invalid_cursor"


def test_one_symbol_carries_its_identity_snapshot_slot(client: TestClient) -> None:
    body = _get(client, f"/v1/symbols/{SYMBOL}").json()
    assert body["data"]["symbol"] == SYMBOL
    # Never synced in this fixture, so the snapshot is absent rather than
    # an error.
    assert body["data"]["info"] is None


def test_a_lowercase_symbol_still_resolves(client: TestClient) -> None:
    """Symbol columns are COLLATE "C", so `testco` and `TESTCO` are
    different values in the database."""
    assert _get(client, "/v1/symbols/testco").status_code == 200


def test_an_unknown_symbol_is_404(client: TestClient) -> None:
    assert _get(client, "/v1/symbols/NOPE").status_code == 404


# --- bars -------------------------------------------------------------------


def test_the_daily_interval_reads_price_history(client: TestClient) -> None:
    """`bars_table_for` used to return periodic_bars for 1d, which is a
    different table with a different key."""
    body = _get(client, f"/v1/symbols/{SYMBOL}/bars", interval="1d", **{"from": "2026-01-01"}).json()
    assert len(body["data"]) == 5
    row = body["data"][0]
    assert row["session_date"] is not None
    assert row["adj_close"] is not None
    assert row["bar_interval"] is None


def test_intraday_hides_extended_hours_BY_DEFAULT(client: TestClient) -> None:
    """Accident prevention, not convenience: extended-hours bars mixed
    into a regular series corrupt every indicator computed from it."""
    body = _get(
        client, f"/v1/symbols/{SYMBOL}/bars", interval="1m", **{"from": "2026-01-05"}
    ).json()
    assert [row["is_extended"] for row in body["data"]] == [False, False, False]


def test_extended_hours_can_be_asked_for(client: TestClient) -> None:
    body = _get(
        client,
        f"/v1/symbols/{SYMBOL}/bars",
        interval="1m",
        session="all",
        **{"from": "2026-01-05"},
    ).json()
    assert len(body["data"]) == 5


def test_the_session_filter_is_REFUSED_above_daily(client: TestClient) -> None:
    """Outside regular hours has no meaning for a weekly bar, and
    periodic_bars has no such column."""
    response = _get(
        client, f"/v1/symbols/{SYMBOL}/bars", interval="1wk", session="all"
    )
    assert response.status_code == 422


def test_weekly_bars_come_from_periodic_bars(client: TestClient) -> None:
    body = _get(
        client, f"/v1/symbols/{SYMBOL}/bars", interval="1wk", **{"from": "2026-01-01"}
    ).json()
    assert len(body["data"]) == 3
    assert body["data"][0]["is_extended"] is None


def test_an_unsupported_interval_is_422(client: TestClient) -> None:
    assert _get(client, f"/v1/symbols/{SYMBOL}/bars", interval="3mo").status_code == 422


def test_an_oversized_range_is_422(client: TestClient) -> None:
    response = _get(
        client,
        f"/v1/symbols/{SYMBOL}/bars",
        interval="1m",
        **{"from": "1980-01-01", "to": "2026-01-01"},
    )
    assert response.status_code == 422
    assert response.json()["type"] == "range_too_large"


def test_prices_cross_the_wire_as_STRINGS(client: TestClient) -> None:
    """Numeric(28,12) exists so prices are not floats; emitting them as
    JSON numbers would undo that at the boundary."""
    body = _get(
        client, f"/v1/symbols/{SYMBOL}/bars", interval="1d", **{"from": "2026-01-01"}
    ).json()
    assert isinstance(body["data"][0]["close"], str)


def test_the_range_is_half_open(client: TestClient) -> None:
    """`to` exclusive, so consecutive pages do not overlap by one row."""
    body = _get(
        client,
        f"/v1/symbols/{SYMBOL}/bars",
        interval="1d",
        **{"from": "2026-01-05", "to": "2026-01-07"},
    ).json()
    assert len(body["data"]) == 2


# --- actions ----------------------------------------------------------------


def test_actions_merge_the_three_sources(client: TestClient) -> None:
    body = _get(
        client, f"/v1/symbols/{SYMBOL}/actions", **{"from": "2026-01-01", "to": "2026-02-01"}
    ).json()
    kinds = {row["action_type"] for row in body["data"]}
    assert kinds == {"DIVIDEND", "SPLIT"}


def test_actions_page_on_a_view_without_a_primary_key(client: TestClient) -> None:
    """A view has no PK, so the sort key is stated explicitly: (date, type)
    is unique per symbol."""
    first = _get(
        client,
        f"/v1/symbols/{SYMBOL}/actions",
        limit=1,
        **{"from": "2026-01-01", "to": "2026-02-01"},
    ).json()
    assert first["next_cursor"]
    second = _get(
        client,
        f"/v1/symbols/{SYMBOL}/actions",
        limit=1,
        cursor=first["next_cursor"],
        **{"from": "2026-01-01", "to": "2026-02-01"},
    ).json()
    assert second["data"][0] != first["data"][0]


# --- authorisation on the read path -----------------------------------------


def test_a_token_without_the_family_scope_is_403(client: TestClient) -> None:
    token, _ = tokens.mint(
        api_settings(), client_id=CLIENT_ID, scopes=("reference:read",), secret_id=1, epoch=0
    )
    response = client.get(
        f"/v1/symbols/{SYMBOL}/bars", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403


def test_scope_is_checked_before_existence(client: TestClient) -> None:
    """Otherwise 404-versus-403 tells an unauthorised caller what exists."""
    token, _ = tokens.mint(
        api_settings(), client_id=CLIENT_ID, scopes=("reference:read",), secret_id=1, epoch=0
    )
    response = client.get(
        "/v1/symbols/NOPE/bars", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403


def test_responses_are_marked_private_and_vary_on_authorization(client: TestClient) -> None:
    """A shared cache holding one client's page and serving it to another
    would be a data leak, not just a stale answer."""
    response = _get(client, "/v1/symbols")
    assert response.headers["Cache-Control"].startswith("private")
    assert "Authorization" in response.headers["Vary"]
    assert response.headers["ETag"]
