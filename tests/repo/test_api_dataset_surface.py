"""The generic dataset surface, end to end."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
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
from yfin.api.storage import catalog
from yfin.api.storage import session as api_session
from yfin.models import Symbol
from yfin.models.holders import HolderBreakdown, HolderType, InstitutionalHolder

pytestmark = pytest.mark.repo

SIGNING_KEY = "k" * 48
CLIENT_ID = "yfc_" + "d" * 32
SYMBOL = "DSETCO"

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


def _token(*scopes: str) -> dict[str, str]:
    token, _ = tokens.mint(
        api_settings(), client_id=CLIENT_ID, scopes=scopes, secret_id=1, epoch=0
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def redis() -> Iterator[fakeredis.FakeRedis]:
    server = fakeredis.FakeRedis(decode_responses=True)
    yield server
    server.flushall()


@pytest.fixture
def seeded(test_engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
    fetched = datetime(2026, 1, 5, tzinfo=UTC)
    with factory() as session:
        session.execute(insert(Symbol), [{"symbol": SYMBOL, "is_active": True}])
        session.execute(
            insert(HolderBreakdown),
            [
                {
                    "symbol": SYMBOL,
                    "as_of_date": date(2026, 1, 5),
                    "insiders_pct_held": Decimal("0.1"),
                    "fetched_at": fetched,
                }
            ],
        )
        session.execute(
            insert(InstitutionalHolder),
            [
                {
                    "symbol": SYMBOL,
                    "as_of_date": date(2026, 1, 5),
                    "holder_type": HolderType.INSTITUTION,
                    "holder": "Big Fund",
                    "shares": 100,
                    "fetched_at": fetched,
                },
                {
                    "symbol": SYMBOL,
                    "as_of_date": date(2026, 1, 5),
                    "holder_type": HolderType.MUTUALFUND,
                    "holder": "Some Mutual",
                    "shares": 50,
                    "fetched_at": fetched,
                },
            ],
        )
        session.commit()
        yield session

        for model in (HolderBreakdown, InstitutionalHolder, Symbol):
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
    for module in (limiter, concurrency, usage, auth_deps):
        monkeypatch.setattr(module, "get_redis", lambda _s: redis)
    monkeypatch.setattr(policy, "limits_for_client", lambda _cid: LIMITS)

    def scope() -> Iterator[Session]:
        with factory() as request_session:
            yield request_session

    app = create_app(api_settings())
    app.dependency_overrides[api_session.session_scope] = scope
    with TestClient(app) as test_client:
        yield test_client


# --- catalogue --------------------------------------------------------------


def test_the_catalogue_is_filtered_to_the_callers_scopes(client: TestClient) -> None:
    """Advertising data the caller cannot fetch is noise; the full list is
    available, but only by asking."""
    reference = client.get("/v1/datasets", headers=_token("reference:read")).json()
    assert {row["family"] for row in reference["data"]} == {"reference"}

    holders = client.get("/v1/datasets", headers=_token("holders:read")).json()
    assert {row["family"] for row in holders["data"]} == {"holders"}
    assert {row["name"] for row in holders["data"]} >= {"major_holders", "mutualfund_holders"}


def test_the_full_catalogue_is_available_on_request(client: TestClient) -> None:
    body = client.get(
        "/v1/datasets", params={"all": True}, headers=_token("reference:read")
    ).json()
    assert body["data"]


def test_the_catalogue_states_what_a_caller_needs_to_page(client: TestClient) -> None:
    body = client.get("/v1/datasets", headers=_token("holders:read")).json()
    entry = next(row for row in body["data"] if row["name"] == "major_holders")
    assert entry["scope"] == "holders:read"
    assert entry["sort_key"] == ["as_of_date"]
    assert entry["symbol_scoped"] is True


# --- data -------------------------------------------------------------------


def test_rows_come_back_through_the_generic_surface(client: TestClient) -> None:
    body = client.get(
        "/v1/datasets/major_holders",
        params={"symbol": SYMBOL},
        headers=_token("holders:read"),
    ).json()
    assert len(body["data"]) == 1
    assert body["data"][0]["symbol"] == SYMBOL


def test_two_datasets_sharing_a_table_DO_NOT_leak_into_each_other(
    client: TestClient,
) -> None:
    """Both write institutional_holders and only holder_type separates
    them. Without the fixed filter, asking for one would return the
    other's rows -- wrong in a way the caller cannot see."""
    institutional = client.get(
        "/v1/datasets/institutional_holders",
        params={"symbol": SYMBOL},
        headers=_token("holders:read"),
    ).json()
    mutual = client.get(
        "/v1/datasets/mutualfund_holders",
        params={"symbol": SYMBOL},
        headers=_token("holders:read"),
    ).json()

    assert [row["holder"] for row in institutional["data"]] == ["Big Fund"]
    assert [row["holder"] for row in mutual["data"]] == ["Some Mutual"]


def test_decimals_are_strings_here_too(client: TestClient) -> None:
    body = client.get(
        "/v1/datasets/major_holders",
        params={"symbol": SYMBOL},
        headers=_token("holders:read"),
    ).json()
    assert isinstance(body["data"][0]["insiders_pct_held"], str)


def test_a_symbol_scoped_dataset_REFUSES_an_unfiltered_scan(client: TestClient) -> None:
    response = client.get("/v1/datasets/major_holders", headers=_token("holders:read"))
    assert response.status_code == 422


def test_an_unknown_filter_is_REFUSED_not_ignored(client: TestClient) -> None:
    """Silently dropping a filter returns more data than was asked for and
    looks like it worked."""
    response = client.get(
        "/v1/datasets/major_holders",
        params={"symbol": SYMBOL, "holder": "x"},
        headers=_token("holders:read"),
    )
    assert response.status_code == 422
    assert response.json()["type"] == "invalid_parameter"


def test_an_unknown_dataset_is_404_for_an_authorised_caller(client: TestClient) -> None:
    response = client.get(
        "/v1/datasets/no_such_dataset", headers=_token("holders:read")
    )
    assert response.status_code == 404


def test_scope_is_checked_before_existence(client: TestClient) -> None:
    """Dataset names are the one part of this surface a caller could
    enumerate, and 404-versus-403 is how they would do it."""
    response = client.get(
        "/v1/datasets/major_holders",
        params={"symbol": SYMBOL},
        headers=_token("reference:read"),
    )
    assert response.status_code == 403


def test_a_dataset_that_declares_nothing_is_INVISIBLE(client: TestClient) -> None:
    """Exposure is opt-in and absence fails closed: registering a dataset
    cannot make it readable, or readable under the wrong scope, by
    accident."""
    from yfin.datasets import SYMBOL_DATASETS

    unexposed = [
        name for name in SYMBOL_DATASETS if not getattr(SYMBOL_DATASETS[name], "api", ())
    ]
    assert unexposed, "fixture assumes at least one dataset opts out"
    assert unexposed[0] not in catalog.CATALOG

    response = client.get(
        f"/v1/datasets/{unexposed[0]}", headers=_token(*[s for s in (
            "reference:read", "bars:read", "fundamentals:read", "holders:read",
            "news:read", "discovery:read", "domains:read")])
    )
    assert response.status_code == 404


def test_paging_uses_the_declared_sort_key(client: TestClient) -> None:
    first = client.get(
        "/v1/datasets/institutional_holders",
        params={"symbol": SYMBOL, "limit": 1},
        headers=_token("holders:read"),
    ).json()
    assert first["data"]
    # One row per holder_type in the fixture, so the institutional slice
    # has exactly one row and no next page.
    assert first["next_cursor"] is None


def test_the_generic_surface_is_metered_like_the_rest(
    client: TestClient, redis: fakeredis.FakeRedis
) -> None:
    """A request served generically must be billed exactly like one served
    by a hand-written endpoint."""
    client.get(
        "/v1/datasets/major_holders",
        params={"symbol": SYMBOL},
        headers=_token("holders:read"),
    )
    counts: dict[str, Any] = redis.hgetall(next(iter(redis.keys("usage:*"))))
    assert counts == {f"{CLIENT_ID}:holders": "1"}


def test_a_calendar_can_be_browsed_without_a_symbol(client: TestClient) -> None:
    """The default refuses an unfiltered scan of a symbol-keyed table, and
    calendars are the documented exception: "what reports this week" is the
    whole point, and each calendar carries an index on its time column, so
    the browse is an index scan rather than a sort over the table."""
    response = client.get(
        "/v1/datasets/earnings_calendar", headers=_token("fundamentals:read")
    )
    assert response.status_code == 200


def test_a_symbol_keyed_dataset_without_that_opt_in_still_refuses(
    client: TestClient,
) -> None:
    response = client.get("/v1/datasets/major_holders", headers=_token("holders:read"))
    assert response.status_code == 422
