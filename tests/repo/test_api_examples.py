"""The published examples, captured from real responses.

Hand-written examples drift from what the API sends, and nothing notices
until a reader follows one. These are captured by calling the API and
committed under `yfin/api/core/examples/`, and `core/openapi.py` attaches
them to the document. Run with `--snapshot-update` to rewrite them.

The document builder reads files, never a database, so
`scripts/dump_openapi.py` stays runnable on any machine with no
environment of its own -- the property its docstring exists to protect.
Producing the files is what needs a database, and that is why this module
carries the `repo` marker while the presence check lives in the unit
suite.

The seed data is written to be READ. It is a fictional company with round
numbers, because these bodies end up in the published contract, and
`TESTCO` with arbitrary decimals is documentation nobody learns from.
Every value that would drift between runs -- the token, the request id,
the validator -- is replaced with a placeholder, or the lock would fail
on every capture and become noise.
"""

from __future__ import annotations

import dataclasses
import json
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
from yfin.api.core.openapi import REQUIRED_EXAMPLES, example_path
from yfin.api.ratelimit import concurrency, limiter, policy, usage
from yfin.api.ratelimit.policy import PlanLimits
from yfin.api.storage import session as api_session
from yfin.models import PriceHistory, Symbol
from yfin.models.financials import (
    FinancialFact,
    FinancialPeriod,
    StatementFreq,
    StatementKind,
)
from yfin.models.holders import HolderBreakdown
from yfin.models.prices import Dividend, Split

pytestmark = pytest.mark.repo

SIGNING_KEY = "k" * 48
CLIENT_ID = "yfc_" + "e" * 32
SYMBOL = "ACME"
DATASET = "major_holders"

ALL_SCOPES = (
    "reference:read",
    "bars:read",
    "fundamentals:read",
    "holders:read",
)

GENEROUS = PlanLimits(
    plan="example",
    requests_per_second=1000,
    burst=1000,
    monthly_quota=1_000_000,
    max_page_size=500,
    max_concurrency=50,
)

#: A plan that refuses the second request in a second, which is the only
#: way to capture a real 429 body rather than write one by hand.
STINGY = dataclasses.replace(GENEROUS, requests_per_second=1, burst=1)

FETCHED = datetime(2026, 1, 5, 22, 0, tzinfo=UTC)


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
    """One fictional company, with a row in every shape the document shows."""
    factory = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
    with factory() as session:
        session.execute(
            insert(Symbol),
            [
                {
                    "symbol": SYMBOL,
                    "isin": "US0000000001",
                    "quote_type": "EQUITY",
                    "exchange": "NMS",
                    "full_exchange_name": "NasdaqGS",
                    "currency": "USD",
                    "timezone": "America/New_York",
                    "short_name": "Acme Corp",
                    "long_name": "Acme Corporation",
                    "first_trade_date": datetime(1998, 5, 4, 13, 30, tzinfo=UTC),
                    "is_active": True,
                }
            ],
        )
        session.execute(
            insert(PriceHistory),
            [
                {
                    "symbol": SYMBOL,
                    "session_date": date(2026, 1, day),
                    "ts_utc": datetime(2026, 1, day, 14, 30, tzinfo=UTC),
                    "open": Decimal("100.00"),
                    "high": Decimal("104.50"),
                    "low": Decimal("99.25"),
                    "close": Decimal("103.75"),
                    "adj_close": Decimal("103.75"),
                    "volume": 1_250_000,
                }
                for day in (5, 6)
            ],
        )
        session.execute(
            insert(Dividend),
            [{"symbol": SYMBOL, "ex_date": date(2026, 1, 6), "amount": Decimal("0.50")}],
        )
        session.execute(
            insert(Split),
            [{"symbol": SYMBOL, "split_date": date(2026, 1, 5), "ratio": Decimal("2")}],
        )
        session.execute(
            insert(FinancialPeriod),
            [
                {
                    "symbol": SYMBOL,
                    "statement": StatementKind.INCOME,
                    "freq": StatementFreq.ANNUAL,
                    "period_end": date(2025, 12, 31),
                    "currency": "USD",
                    "raw_json": "{}",
                    "content_hash": "0" * 64,
                    "fetched_at": FETCHED,
                }
            ],
        )
        session.execute(
            insert(FinancialFact),
            [
                {
                    "symbol": SYMBOL,
                    "statement": StatementKind.INCOME,
                    "freq": StatementFreq.ANNUAL,
                    "period_end": date(2025, 12, 31),
                    "item_key": item,
                    "value": value,
                }
                for item, value in (
                    ("TotalRevenue", Decimal("48000000000")),
                    ("NetIncome", Decimal("7200000000")),
                )
            ],
        )
        session.execute(
            insert(HolderBreakdown),
            [
                {
                    "symbol": SYMBOL,
                    "as_of_date": date(2026, 1, 5),
                    "insiders_pct_held": Decimal("0.0412"),
                    "institutions_pct_held": Decimal("0.7310"),
                    "institutions_float_pct_held": Decimal("0.7624"),
                    "institutions_count": 3120,
                    "fetched_at": FETCHED,
                }
            ],
        )
        session.commit()
        yield session

        # Children before parents: everything here references symbols.
        for model in (
            HolderBreakdown,
            FinancialFact,
            FinancialPeriod,
            Dividend,
            Split,
            PriceHistory,
            Symbol,
        ):
            session.query(model).delete()
        session.commit()


def _make_client(
    test_engine: Engine,
    redis: fakeredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
    limits: PlanLimits,
) -> Iterator[TestClient]:
    factory = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(api_session, "get_session_factory", lambda: factory)
    for module in (limiter, concurrency, usage, auth_deps):
        monkeypatch.setattr(module, "get_redis", lambda _s: redis)
    monkeypatch.setattr(policy, "limits_for_client", lambda _cid: limits)

    def scope() -> Iterator[Session]:
        with factory() as request_session:
            yield request_session

    app = create_app(api_settings())
    app.dependency_overrides[api_session.session_scope] = scope
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def client(
    seeded: Session,
    test_engine: Engine,
    redis: fakeredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    yield from _make_client(test_engine, redis, monkeypatch, GENEROUS)


@pytest.fixture
def stingy_client(
    seeded: Session,
    test_engine: Engine,
    redis: fakeredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    yield from _make_client(test_engine, redis, monkeypatch, STINGY)


# --- normalisation ----------------------------------------------------------

#: What a placeholder looks like in a published example. Chosen to be
#: obviously not a real value, so nobody copies one into a request.
PLACEHOLDERS = {
    "access_token": "eyJhbGciOiJIUzI1NiIsImtpZCI6ImsxIn0.<payload>.<signature>",
    "request_id": "0123456789abcdef0123456789abcdef",
    "next_cursor": "<opaque cursor>",
}


def normalise(body: Any) -> Any:
    """Replaces what drifts between runs, and nothing else.

    Anything not on this list that changes from one capture to the next is
    a finding about the API, not a nuisance to paper over: it means a
    response carries a value nobody meant to make part of the contract.
    """
    if isinstance(body, list):
        return [normalise(item) for item in body]
    if not isinstance(body, dict):
        return body
    out = {}
    for key, value in body.items():
        if key in PLACEHOLDERS and value is not None:
            out[key] = PLACEHOLDERS[key]
        else:
            out[key] = normalise(value)
    return out


def _check(request: pytest.FixtureRequest, operation: str, status: int, name: str,
           response: Any) -> None:
    """Compares one captured body against the committed file, or writes it."""
    assert response.status_code == status, (
        f"{operation}.{status}.{name}: got {response.status_code} {response.text[:200]}"
    )
    captured = normalise(response.json())
    path = example_path(operation, status, name)

    if request.config.getoption("--snapshot-update"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(captured, indent=2, sort_keys=True) + "\n", "utf-8")
        return

    assert path.is_file(), (
        f"{path.name} is missing; capture it with "
        "pytest -m repo tests/repo/test_api_examples.py --snapshot-update"
    )
    assert json.loads(path.read_text("utf-8")) == captured, (
        f"{path.name} no longer matches what the API sends; review the change and "
        "re-capture with --snapshot-update"
    )


# --- capture ----------------------------------------------------------------


def test_the_token_examples(
    client: TestClient, request: pytest.FixtureRequest, seeded: Session
) -> None:
    """Captured from the shapes the endpoint answers without a client row:
    a malformed request and a rejected identity. The 200 is minted rather
    than fetched, because a real one would need a credential in the
    fixture and the body is the same either way."""
    _check(
        request,
        "issueToken",
        400,
        "invalid_request",
        client.post("/oauth/token", data={"grant_type": "client_credentials"}),
    )
    _check(
        request,
        "issueToken",
        401,
        "invalid_client",
        client.post(
            "/oauth/token",
            data={"grant_type": "client_credentials"},
            auth=(CLIENT_ID, "not-the-secret"),
        ),
    )

    token, expires_in = tokens.mint(
        api_settings(), client_id=CLIENT_ID, scopes=ALL_SCOPES, secret_id=1, epoch=0
    )
    minted = {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "scope": " ".join(ALL_SCOPES),
    }
    _check(request, "issueToken", 200, "token", _Fake(200, minted))


def test_the_health_example(client: TestClient, request: pytest.FixtureRequest) -> None:
    _check(request, "getHealth", 200, "up", client.get("/health"))


def test_the_symbol_examples(
    client: TestClient, request: pytest.FixtureRequest
) -> None:
    auth = _token(*ALL_SCOPES)
    _check(request, "listSymbols", 200, "page", client.get("/v1/symbols", headers=auth))
    _check(request, "listSymbols", 401, "unauthenticated", client.get("/v1/symbols"))
    _check(
        request,
        "getSymbol",
        200,
        "symbol",
        client.get(f"/v1/symbols/{SYMBOL}", headers=auth),
    )
    _check(
        request,
        "getSymbol",
        404,
        "not_found",
        client.get("/v1/symbols/NOSUCH", headers=auth),
    )


def test_the_bars_and_actions_examples(
    client: TestClient, request: pytest.FixtureRequest
) -> None:
    auth = _token(*ALL_SCOPES)
    _check(
        request,
        "listBars",
        200,
        "page",
        client.get(
            f"/v1/symbols/{SYMBOL}/bars",
            params={"interval": "1d", "from": "2026-01-01T00:00:00Z"},
            headers=auth,
        ),
    )
    _check(
        request,
        "listBars",
        422,
        "range_too_large",
        client.get(
            f"/v1/symbols/{SYMBOL}/bars",
            # Both ends: with only `from`, the window is clamped to the
            # interval's cap instead of exceeding it.
            params={
                "interval": "1m",
                "from": "2020-01-01T00:00:00Z",
                "to": "2026-01-01T00:00:00Z",
            },
            headers=auth,
        ),
    )
    _check(
        request,
        "listActions",
        200,
        "page",
        client.get(f"/v1/symbols/{SYMBOL}/actions", headers=auth),
    )


def test_the_financials_example(
    client: TestClient, request: pytest.FixtureRequest
) -> None:
    _check(
        request,
        "listFinancials",
        200,
        "page",
        client.get(
            f"/v1/symbols/{SYMBOL}/financials",
            params={"statement": "income", "freq": "annual"},
            headers=_token(*ALL_SCOPES),
        ),
    )


def test_the_dataset_examples(
    client: TestClient, request: pytest.FixtureRequest
) -> None:
    auth = _token(*ALL_SCOPES)
    _check(
        request,
        "listDatasets",
        200,
        "catalogue",
        # A token with one narrow scope, so the example is a real response
        # that a reader can take in. The catalogue does not page, and the
        # full fifty-five entries with their columns are ninety kilobytes
        # of documentation nobody reads.
        client.get("/v1/datasets", headers=_token("news:read")),
    )
    _check(
        request,
        "readDataset",
        200,
        "page",
        client.get(f"/v1/datasets/{DATASET}", params={"symbol": SYMBOL}, headers=auth),
    )
    _check(
        request,
        "readDataset",
        403,
        "insufficient_scope",
        client.get(
            f"/v1/datasets/{DATASET}",
            params={"symbol": SYMBOL},
            headers=_token("reference:read"),
        ),
    )
    _check(
        request,
        "readDataset",
        404,
        "not_found",
        client.get("/v1/datasets/no_such_dataset", headers=auth),
    )
    _check(
        request,
        "readDataset",
        422,
        "invalid_parameter",
        client.get(
            f"/v1/datasets/{DATASET}",
            params={"symbol": SYMBOL, "colour": "blue"},
            headers=auth,
        ),
    )
    _check(
        request,
        "readDataset",
        422,
        "invalid_cursor",
        client.get(
            f"/v1/datasets/{DATASET}",
            params={"symbol": SYMBOL, "cursor": "not-a-cursor"},
            headers=auth,
        ),
    )


def test_the_rate_limit_example(
    stingy_client: TestClient, request: pytest.FixtureRequest
) -> None:
    """A real 429 body, from a plan that allows one request a second."""
    auth = _token(*ALL_SCOPES)
    stingy_client.get("/v1/symbols", headers=auth)
    _check(
        request,
        "listSymbols",
        429,
        "rate_limit_exceeded",
        stingy_client.get("/v1/symbols", headers=auth),
    )


def test_every_required_example_was_captured() -> None:
    """Catches a table entry nothing above produces. The unit suite asserts
    the files exist; this asserts the capture covers the table, so the two
    cannot agree on a file neither of them writes."""
    missing = [
        f"{operation}.{status}.{name}"
        for operation, statuses in REQUIRED_EXAMPLES.items()
        for status, names in statuses.items()
        for name in names
        if not example_path(operation, status, name).is_file()
    ]
    assert missing == []


class _Fake:
    """A response that was built rather than received.

    One example needs this: a 200 from `/oauth/token` requires a client row
    with a hashed secret, which is the credential store's test to write,
    not this module's. The body is `TokenResponse` either way, and the
    token itself is a placeholder in the published example.
    """

    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)

    def json(self) -> Any:
        return self._body
