"""`GET /ui/api/symbols/{symbol}/news`, end to end against a real schema.

Copies the `seeded`/`client` pattern from `test_api_read_endpoints.py`,
with the differences the UI route requires: `ApiSettings` carries
`ui_enabled=True, ui_password=PW` (otherwise `create_app` never installs
the UI and the route is a plain 404); `pages.default_dist_dir` is
monkeypatched to an absent directory (no build is needed to exercise the
data routes); and identity is the session cookie, not a Bearer token.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
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
from yfin.models.news import News, NewsSymbol
from yfin.ui import pages, session
from yfin.ui.session import COOKIE_NAME

pytestmark = pytest.mark.repo

SIGNING_KEY = "k" * 48
PW = "hunter2"


def api_settings() -> ApiSettings:
    return ApiSettings(
        jwt_signing_key=SIGNING_KEY,
        jwt_kid="k1",
        jwt_issuer="yfin-api",
        jwt_audience="yfin-api",
        ui_enabled=True,
        ui_password=PW,
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
    with factory() as session_:
        session_.execute(
            insert(News),
            [
                {
                    "news_id": "n-older",
                    "title": "Older AAPL story",
                    "pub_date": base,
                    "click_through_url": "https://example.com/click",
                    "raw_json": "{}",
                },
                {
                    "news_id": "n-newer",
                    "title": "Newer AAPL story",
                    "pub_date": base + timedelta(hours=1),
                    "canonical_url": "https://example.com/canonical",
                    "raw_json": "{}",
                },
                {
                    "news_id": "n-msft",
                    "title": "MSFT story",
                    "pub_date": base + timedelta(hours=2),
                    "raw_json": "{}",
                },
            ],
        )
        session_.execute(
            insert(NewsSymbol),
            [
                {"news_id": "n-older", "symbol": "AAPL"},
                {"news_id": "n-newer", "symbol": "AAPL"},
                {"news_id": "n-msft", "symbol": "MSFT"},
            ],
        )
        session_.commit()
        yield session_

        # Children before parents: news_symbols references news.
        for model in (NewsSymbol, News):
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
        token, _ = session.issue(api_settings())
        test_client.cookies.set(COOKIE_NAME, token)
        yield test_client


def test_news_is_joined_per_symbol_newest_first(client: TestClient, seeded: Session) -> None:
    body = client.get("/ui/api/symbols/AAPL/news").json()
    ids = [row["news_id"] for row in body["data"]]
    assert ids == ["n-newer", "n-older"]  # MSFT's article absent
    assert body["data"][0]["link"] == "https://example.com/canonical"
    assert body["data"][1]["link"] == "https://example.com/click"  # no canonical -> click-through
    assert body["as_of"] is None and body["next_cursor"] is None


def test_news_limit_caps_the_list(client: TestClient, seeded: Session) -> None:
    body = client.get("/ui/api/symbols/AAPL/news", params={"limit": 1}).json()
    assert [row["news_id"] for row in body["data"]] == ["n-newer"]
