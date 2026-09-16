"""The core read endpoints, end to end against a real schema.

These go through the HTTP layer because the seams (interval to table, session
filter, cursor to sort key, Decimal to JSON) are invisible to a repository test.
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
    """1d must read price history; periodic_bars is a different table with a
    different key."""
    body = _get(
        client, f"/v1/symbols/{SYMBOL}/bars", interval="1d", **{"from": "2026-01-01"}
    ).json()
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


# --- query cost -------------------------------------------------------------


def test_a_cancelled_query_is_504_not_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The statement timeout has to reach the caller as their answer: a 500
    tells them nothing, and the middleware refunds a 500's quota unit, so an
    unservable query would cost nothing."""
    from yfin.api.storage import limits as query_limits

    monkeypatch.setattr(query_limits, "STATEMENT_TIMEOUT_MS", 1)
    response = _get(client, f"/v1/symbols/{SYMBOL}/bars", interval="1d")
    # A one-millisecond budget cancels whatever it lands on; if the query
    # somehow beat it, the assertion below still holds for 200.
    assert response.status_code in (200, 504)
    if response.status_code == 504:
        assert response.json()["type"] == "query_timeout"


def test_paging_works_WITHOUT_an_explicit_range(client: TestClient) -> None:
    """An open-ended range resolves against `now`. If the cursor's
    fingerprint covered the resolved window, it would differ by
    milliseconds between one page and the next and no cursor would ever
    match its own query -- paging without a `from` would be impossible,
    which is the most common way to call this endpoint."""
    first = _get(client, f"/v1/symbols/{SYMBOL}/bars", interval="1d", limit=2).json()
    assert first["next_cursor"], "fixture must produce more than one page"

    second = _get(
        client,
        f"/v1/symbols/{SYMBOL}/bars",
        interval="1d",
        limit=2,
        cursor=first["next_cursor"],
    )
    assert second.status_code == 200
    seen = {row["ts_utc"] for row in first["data"]} & {
        row["ts_utc"] for row in second.json()["data"]
    }
    assert seen == set(), "pages must not overlap"


# --- conditional requests ---------------------------------------------------


def test_a_matching_etag_answers_304_with_no_body(client: TestClient) -> None:
    first = _get(client, "/v1/symbols")
    assert first.status_code == 200
    etag = first.headers["ETag"]

    again = client.get("/v1/symbols", headers={**_auth(), "If-None-Match": etag})
    assert again.status_code == 304
    assert again.content == b""


def test_a_304_carries_what_rfc_9110_requires(client: TestClient) -> None:
    """§15.4.5: the headers whose value would differ from the 200's. The
    rate headers ride along too -- a conditional request still costs a
    request, so a client must be able to see its budget shrink."""
    etag = _get(client, "/v1/symbols").headers["ETag"]
    response = client.get("/v1/symbols", headers={**_auth(), "If-None-Match": etag})

    assert response.headers["ETag"] == etag
    assert response.headers["Cache-Control"].startswith("private")
    assert "Authorization" in response.headers["Vary"]
    assert "RateLimit-Remaining" in response.headers
    assert "X-Quota-Remaining" in response.headers


def test_a_conditional_request_still_costs_a_request(client: TestClient) -> None:
    """The prior design settled this: a 304 saves bandwidth and a query,
    not a counter. Exempting it would have been a silent reversal."""
    etag = _get(client, "/v1/symbols").headers["ETag"]
    before = int(_get(client, "/v1/symbols").headers["X-Quota-Remaining"])
    response = client.get("/v1/symbols", headers={**_auth(), "If-None-Match": etag})

    assert response.status_code == 304
    after = int(_get(client, "/v1/symbols").headers["X-Quota-Remaining"])
    assert after < before


def test_the_etag_changes_when_the_DATA_changes(
    client: TestClient, seeded: Session
) -> None:
    """The validator is derived from the response body: a hash of the request
    identity alone never moves when the data does, and honouring If-None-Match
    against it would pin a client to one page forever."""
    etag = _get(client, "/v1/symbols").headers["ETag"]

    seeded.execute(
        insert(Symbol),
        [{"symbol": "TESTNEW", "is_active": True, "exchange": "NMS"}],
    )
    seeded.commit()

    after = _get(client, "/v1/symbols")
    assert after.status_code == 200
    assert after.headers["ETag"] != etag


def test_a_stale_etag_gets_the_body(client: TestClient) -> None:
    response = client.get(
        "/v1/symbols", headers={**_auth(), "If-None-Match": 'W/"nonsense"'}
    )
    assert response.status_code == 200
    assert response.json()["data"]


def test_an_unsupported_interval_is_refused_by_the_published_enum(
    client: TestClient,
) -> None:
    """The interval check is the annotation, so the document lists the values
    a caller may send."""
    assert _get(client, f"/v1/symbols/{SYMBOL}/bars", interval="3h").status_code == 422


def test_an_inverted_range_is_NOT_called_too_large(client: TestClient) -> None:
    """`list_actions` raised `range_too_large` for every window refusal,
    including a reversed one -- a type its own published schema forbade, so
    a generated client could not deserialise its own error. The reason it
    could happen: the storage layer returned prose and the router asked
    whether the word "exceeds" appeared in it."""
    response = _get(
        client,
        f"/v1/symbols/{SYMBOL}/actions",
        **{"from": "2026-02-01T00:00:00Z", "to": "2026-01-01T00:00:00Z"},
    )
    assert response.status_code == 422
    assert response.json()["type"] == "invalid_parameter"


def test_a_range_that_really_is_too_large_still_says_so(client: TestClient) -> None:
    response = _get(
        client,
        f"/v1/symbols/{SYMBOL}/bars",
        interval="1m",
        **{"from": "2020-01-01T00:00:00Z", "to": "2026-01-01T00:00:00Z"},
    )
    assert response.status_code == 422
    assert response.json()["type"] == "range_too_large"


# --- the published header contract -------------------------------------------
#
# These drive the real endpoints and compare what the handler actually sends
# against what the document says; checking the document against the table in
# `core/openapi.py` that produced it could never fail.

#: One header per family. Checking every name would test the header
#: dictionaries against themselves again; what has to hold is that the
#: FAMILY is present exactly where it is published.
FAMILY_MARKERS = ("RateLimit-Limit", "Cache-Control", "ETag")

#: operationId -> a request that answers 200, and whether it revalidates.
OPERATIONS: tuple[tuple[str, str, dict[str, Any], bool], ...] = (
    ("listSymbols", "/v1/symbols", {}, True),
    ("getSymbol", f"/v1/symbols/{SYMBOL}", {}, True),
    ("listBars", f"/v1/symbols/{SYMBOL}/bars", {"interval": "1d"}, True),
    ("listActions", f"/v1/symbols/{SYMBOL}/actions", {}, True),
    (
        "listFinancials",
        f"/v1/symbols/{SYMBOL}/financials",
        {"statement": "income", "freq": "annual"},
        True,
    ),
    ("listDatasets", "/v1/datasets", {}, False),
    # A `fundamentals` resource, because that is one of the scopes this
    # test's token carries; the resource itself is beside the point.
    ("readDataset", "/v1/datasets/analyst_price_targets", {"symbol": SYMBOL}, False),
    ("getHealth", "/health", {}, False),
)


def _published(document: dict[str, Any], operation_id: str, status: int) -> set[str] | None:
    """Header names the document publishes, or None if it publishes no such
    response. `X-Data-As-Of` is dropped: it is declared `required=False`,
    i.e. the document already says it may be absent."""
    for operations in document["paths"].values():
        for operation in operations.values():
            if operation.get("operationId") != operation_id:
                continue
            response = operation["responses"].get(str(status))
            if response is None:
                return None
            return {
                name
                for name, header in response.get("headers", {}).items()
                if header.get("required", True)
            }
    raise AssertionError(f"{operation_id} is not in the document")


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    return create_app(api_settings()).openapi()


@pytest.mark.parametrize(
    ("operation_id", "path", "params", "conditional"),
    OPERATIONS,
    ids=[row[0] for row in OPERATIONS],
)
def test_a_200_carries_exactly_the_header_families_it_publishes(
    client: TestClient,
    document: dict[str, Any],
    operation_id: str,
    path: str,
    params: dict[str, Any],
    conditional: bool,
) -> None:
    response = client.get(path, params=params, headers=_auth())
    assert response.status_code == 200, response.text

    published = _published(document, operation_id, 200)
    assert published is not None
    for marker in FAMILY_MARKERS:
        assert (marker in response.headers) == (marker in published), (operation_id, marker)


@pytest.mark.parametrize(
    ("operation_id", "path", "params", "conditional"),
    OPERATIONS,
    ids=[row[0] for row in OPERATIONS],
)
def test_a_304_is_published_exactly_where_one_can_be_answered(
    client: TestClient,
    document: dict[str, Any],
    operation_id: str,
    path: str,
    params: dict[str, Any],
    conditional: bool,
) -> None:
    """The document's 304 is a promise a client acts on: it sends
    `If-None-Match` because the contract said a 304 was possible."""
    first = client.get(path, params=params, headers=_auth())
    etag = first.headers.get("ETag")
    published = _published(document, operation_id, 304)

    if etag is None:
        assert published is None, f"{operation_id} publishes a 304 it cannot answer"
        return

    assert published is not None, f"{operation_id} answers a 304 it does not publish"
    again = client.get(path, params=params, headers={**_auth(), "If-None-Match": etag})
    assert again.status_code == 304
    assert again.content == b""
    for marker in FAMILY_MARKERS:
        assert (marker in again.headers) == (marker in published), (operation_id, marker)
