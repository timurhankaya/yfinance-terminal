"""`GET /ui/api/search` against a real schema.

The ranking is SQL, so only a database can answer whether it is right.
The `seeded`/`client` pattern of `test_ui_sparklines_repo.py`.
"""

from __future__ import annotations

from collections.abc import Iterator
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
from yfin.models.symbols import Symbol
from yfin.ui import pages

pytestmark = pytest.mark.repo

SIGNING_KEY = "k" * 48

#: The rows that made each ordering rule necessary.
UNIVERSE = [
    ("AAPL", "Apple Inc.", True),
    ("AAPL.MX", "Apple Inc.", True),
    ("4AAPL.TI", "APPLE", True),
    ("APPLE31391-USD", "dog with apple in mouth USD", True),
    ("0P000186HG", "Appletree Subordinatd Debt A", True),
    ("AKBNK.IS", "Akbank T.A.S.", True),
    ("MSFT", "Microsoft Corporation", True),
    ("DELISTED", "Apple Delisted Ltd", False),
]


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


@pytest.fixture
def seeded(test_engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
    with factory() as session_:
        session_.execute(
            insert(Symbol),
            [
                {"symbol": symbol, "long_name": name, "is_active": active}
                for symbol, name, active in UNIVERSE
            ],
        )
        session_.commit()
        yield session_

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


def hits(client: TestClient, query: str) -> list[str]:
    response = client.get("/ui/api/search", params={"q": query})
    assert response.status_code == 200, response.text
    return [row["symbol"] for row in response.json()["data"]]


def test_a_company_name_finds_a_ticker_nobody_could_guess(client: TestClient) -> None:
    """The whole reason the route exists.

    `/v1/symbols?q=` matches the symbol column, so AKBANK returns nothing
    while Akbank sits in the table under `AKBNK.IS`.
    """
    assert hits(client, "AKBANK") == ["AKBNK.IS"]
    assert hits(client, "microsoft") == ["MSFT"]


def test_an_exact_ticker_wins_outright(client: TestClient) -> None:
    assert hits(client, "AAPL")[0] == "AAPL"


def test_a_name_that_starts_with_the_query_beats_a_ticker_that_does(
    client: TestClient,
) -> None:
    """A name prefix match outranks a ticker prefix match."""
    found = hits(client, "APPLE")
    assert found[0] == "AAPL"
    assert found.index("AAPL") < found.index("APPLE31391-USD")


def test_the_primary_listing_comes_before_the_same_company_elsewhere(
    client: TestClient,
) -> None:
    """A suffix is the exchange's: AAPL before AAPL.MX, and before
    `4AAPL.TI`, whose name is the single word APPLE and which a
    shortest-name tie-break alone had put first."""
    found = hits(client, "APPLE")
    assert found.index("AAPL") < found.index("AAPL.MX")
    assert found.index("AAPL") < found.index("4AAPL.TI")


def test_the_tightest_name_breaks_the_remaining_tie(client: TestClient) -> None:
    """Both names start with APPLE; ordering that band by symbol alone
    put "Appletree Subordinatd Debt A" first."""
    found = hits(client, "APPLE")
    assert found.index("AAPL") < found.index("0P000186HG")


def test_an_inactive_symbol_is_not_offered(client: TestClient) -> None:
    """An inactive row is one discovery found and nothing fetches, so
    picking it would open an empty panel."""
    assert "DELISTED" not in hits(client, "APPLE")


def test_one_character_is_refused_rather_than_scanning_the_universe(
    client: TestClient,
) -> None:
    response = client.get("/ui/api/search", params={"q": "A"})
    assert response.status_code == 422
    assert "at least" in response.json()["detail"]
