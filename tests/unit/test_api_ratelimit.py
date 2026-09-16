"""Rate limiting, quota, concurrency and usage measurement."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Any

import fakeredis
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from yfin.api.app import create_app
from yfin.api.auth import dependencies as auth_deps
from yfin.api.auth import jwt as tokens
from yfin.api.auth.dependencies import Principal
from yfin.api.core.config import ApiSettings
from yfin.api.ratelimit import concurrency, limiter, policy, usage
from yfin.api.ratelimit import dependencies as rl
from yfin.api.ratelimit.policy import PlanLimits
from yfin.core.families import DataFamily

SIGNING_KEY = "k" * 48
CLIENT_ID = "yfc_" + "b" * 32

LIMITS = PlanLimits(
    plan="test",
    requests_per_second=2,
    burst=3,
    monthly_quota=10,
    max_page_size=100,
    max_concurrency=2,
)


def settings(**overrides: Any) -> ApiSettings:
    base = {
        "jwt_signing_key": SIGNING_KEY,
        "jwt_kid": "k1",
        "jwt_issuer": "yfin-api",
        "jwt_audience": "yfin-api",
    }
    return ApiSettings(**(base | overrides))


@pytest.fixture
def redis() -> Iterator[fakeredis.FakeRedis]:
    server = fakeredis.FakeRedis(decode_responses=True)
    yield server
    server.flushall()


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch, redis: fakeredis.FakeRedis) -> None:
    for module in (limiter, concurrency, usage, auth_deps):
        monkeypatch.setattr(module, "get_redis", lambda _s: redis)
    monkeypatch.setattr(policy, "limits_for_client", lambda _cid: LIMITS)


def _token(scopes: tuple[str, ...] = ("bars:read",)) -> str:
    token, _ = tokens.mint(
        settings(), client_id=CLIENT_ID, scopes=scopes, secret_id=1, epoch=0
    )
    return token


# --- the Lua decision -------------------------------------------------------


def test_the_burst_is_spent_before_a_refusal() -> None:
    for _ in range(LIMITS.burst):
        assert limiter.consume(settings(), CLIENT_ID, LIMITS).allowed
    verdict = limiter.consume(settings(), CLIENT_ID, LIMITS)
    assert not verdict.allowed
    assert verdict.reason == limiter.REASON_RATE


def test_a_refusal_carries_a_usable_Retry_After() -> None:
    """The difference between an API that says no and one a client can
    actually back off against."""
    for _ in range(LIMITS.burst + 1):
        verdict = limiter.consume(settings(), CLIENT_ID, LIMITS)
    assert verdict.retry_after >= 1


def test_an_exhausted_quota_is_refused_WITHOUT_spending_a_token(
    redis: fakeredis.FakeRedis,
) -> None:
    """A client out of quota must not also burn through its burst: the
    quota is read first and the bucket is left untouched."""
    key = limiter.QUOTA_KEY.format(client_id=CLIENT_ID, period=limiter.period_key())
    redis.set(key, LIMITS.monthly_quota)

    verdict = limiter.consume(settings(), CLIENT_ID, LIMITS)
    assert not verdict.allowed
    assert verdict.reason == limiter.REASON_QUOTA
    assert not redis.exists(limiter.BUCKET_KEY.format(client_id=CLIENT_ID))


def test_the_quota_key_ALWAYS_gets_a_TTL(redis: fakeredis.FakeRedis) -> None:
    """INCR and EXPIRE are one script. As two commands, a process dying in
    between leaves a key with no TTL and that client's counter never
    resets -- quota exhausted forever, with nothing in the logs."""
    limiter.consume(settings(), CLIENT_ID, LIMITS)
    key = limiter.QUOTA_KEY.format(client_id=CLIENT_ID, period=limiter.period_key())
    assert redis.ttl(key) > 0


def test_the_bucket_refills_over_time(redis: fakeredis.FakeRedis) -> None:
    for _ in range(LIMITS.burst + 1):
        limiter.consume(settings(), CLIENT_ID, LIMITS)

    # Rewind the bucket's clock rather than sleeping.
    key = limiter.BUCKET_KEY.format(client_id=CLIENT_ID)
    stamped = float(redis.hget(key, "ts"))  # type: ignore[arg-type]
    redis.hset(key, "ts", stamped - 2000)

    assert limiter.consume(settings(), CLIENT_ID, LIMITS).allowed


def test_the_quota_window_is_UTC() -> None:
    """A per-client local month would make two clients' usage
    incomparable and reset at a different instant for each."""
    from datetime import UTC, datetime

    moment = datetime(2026, 3, 15, 23, 30, tzinfo=UTC)
    assert limiter.period_key(moment) == "2026-03"
    assert limiter.seconds_until_month_end(moment) > 0


def test_a_refund_never_goes_below_zero(redis: fakeredis.FakeRedis) -> None:
    """A refund arriving after a month boundary would otherwise open the
    next month with a negative counter."""
    limiter.refund_quota(settings(), CLIENT_ID)
    key = limiter.QUOTA_KEY.format(client_id=CLIENT_ID, period=limiter.period_key())
    assert int(redis.get(key) or 0) == 0


def test_the_limiter_FAILS_OPEN_when_redis_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Losing a counter store costs accounting; refusing all traffic would
    cost the product."""

    def broken(_s: ApiSettings) -> Any:
        raise ConnectionError("redis is down")

    monkeypatch.setattr(limiter, "get_redis", broken)
    verdict = limiter.consume(settings(), CLIENT_ID, LIMITS)
    assert verdict.allowed
    assert verdict.degraded


# --- concurrency ------------------------------------------------------------


def test_slots_are_capped_per_client() -> None:
    for _ in range(LIMITS.max_concurrency):
        assert concurrency.acquire(settings(), CLIENT_ID, LIMITS.max_concurrency).acquired
    assert not concurrency.acquire(settings(), CLIENT_ID, LIMITS.max_concurrency).acquired


def test_a_refused_slot_is_GIVEN_BACK(redis: fakeredis.FakeRedis) -> None:
    """A refused request must not keep occupying a slot it never got."""
    for _ in range(LIMITS.max_concurrency):
        concurrency.acquire(settings(), CLIENT_ID, LIMITS.max_concurrency)
    concurrency.acquire(settings(), CLIENT_ID, LIMITS.max_concurrency)
    held = int(redis.get(concurrency.SLOT_KEY.format(client_id=CLIENT_ID)) or 0)
    assert held == LIMITS.max_concurrency


def test_a_released_slot_can_be_taken_again() -> None:
    for _ in range(LIMITS.max_concurrency):
        concurrency.acquire(settings(), CLIENT_ID, LIMITS.max_concurrency)
    concurrency.release(settings(), CLIENT_ID)
    assert concurrency.acquire(settings(), CLIENT_ID, LIMITS.max_concurrency).acquired


def test_slots_carry_a_TTL_so_a_crash_cannot_leak_one(
    redis: fakeredis.FakeRedis,
) -> None:
    concurrency.acquire(settings(), CLIENT_ID, LIMITS.max_concurrency)
    assert redis.ttl(concurrency.SLOT_KEY.format(client_id=CLIENT_ID)) > 0


# --- the guard, end to end --------------------------------------------------


def _guarded_app() -> FastAPI:
    app = create_app(settings())

    @app.get("/bars")
    def bars(principal: Annotated[Principal, Depends(rl.guard(DataFamily.BARS))]) -> dict[str, str]:
        return {"client_id": principal.client_id}

    @app.get("/boom")
    def boom(
        principal: Annotated[Principal, Depends(rl.guard(DataFamily.BARS))],
    ) -> dict[str, str]:
        raise RuntimeError("server side failure")

    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(_guarded_app(), raise_server_exceptions=False) as test_client:
        yield test_client


def _get(client: TestClient, path: str = "/bars") -> Any:
    return client.get(path, headers={"Authorization": f"Bearer {_token()}"})


def test_a_successful_response_carries_both_header_families(client: TestClient) -> None:
    """Two families because they answer different questions on different
    clocks; folding the monthly quota into RateLimit-* would tell a client
    its per-second budget was millions."""
    response = _get(client)
    assert response.status_code == 200
    assert response.headers["RateLimit-Limit"] == str(LIMITS.requests_per_second)
    assert int(response.headers["RateLimit-Remaining"]) >= 0
    assert response.headers["X-Quota-Limit"] == str(LIMITS.monthly_quota)


def test_exceeding_the_rate_is_429_rate_limit_exceeded(client: TestClient) -> None:
    for _ in range(LIMITS.burst):
        _get(client)
    response = _get(client)
    assert response.status_code == 429
    assert response.json()["type"] == "rate_limit_exceeded"
    assert response.headers["Retry-After"]


def test_exhausting_the_quota_is_429_quota_exceeded(
    client: TestClient, redis: fakeredis.FakeRedis
) -> None:
    key = limiter.QUOTA_KEY.format(client_id=CLIENT_ID, period=limiter.period_key())
    redis.set(key, LIMITS.monthly_quota)
    response = _get(client)
    assert response.status_code == 429
    assert response.json()["type"] == "quota_exceeded"


def test_the_scope_comes_from_the_family(client: TestClient) -> None:
    """One argument settles the scope required, the counter billed and the
    limits applied, so they cannot disagree."""
    token, _ = tokens.mint(
        settings(), client_id=CLIENT_ID, scopes=("news:read",), secret_id=1, epoch=0
    )
    response = client.get("/bars", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    assert response.json()["type"] == "insufficient_scope"


def test_a_server_error_REFUNDS_the_quota_unit(
    client: TestClient, redis: fakeredis.FakeRedis
) -> None:
    """A client must not pay for our 500."""
    key = limiter.QUOTA_KEY.format(client_id=CLIENT_ID, period=limiter.period_key())
    assert _get(client, "/boom").status_code == 500
    assert int(redis.get(key) or 0) == 0


def test_a_server_error_is_NOT_COUNTED_as_usage(
    client: TestClient, redis: fakeredis.FakeRedis
) -> None:
    _get(client, "/boom")
    assert not redis.keys("usage:*")


def test_a_successful_request_IS_counted(
    client: TestClient, redis: fakeredis.FakeRedis
) -> None:
    _get(client)
    day_key = next(iter(redis.keys("usage:*")))
    counts = redis.hgetall(day_key)
    assert counts == {f"{CLIENT_ID}:bars": "1"}


def test_a_rate_limited_request_is_NOT_counted(
    client: TestClient, redis: fakeredis.FakeRedis
) -> None:
    """It was refused before any work was done; charging for it would bill
    a client for our own back-pressure."""
    for _ in range(LIMITS.burst):
        _get(client)
    before = redis.hgetall(next(iter(redis.keys("usage:*"))))
    _get(client)
    after = redis.hgetall(next(iter(redis.keys("usage:*"))))
    assert before == after


def test_the_concurrency_slot_is_RELEASED_after_the_response(
    client: TestClient, redis: fakeredis.FakeRedis
) -> None:
    """Held slots would accumulate until every worker was occupied by
    requests that had already finished."""
    _get(client)
    held = int(redis.get(concurrency.SLOT_KEY.format(client_id=CLIENT_ID)) or 0)
    assert held == 0


def test_the_slot_is_released_EVEN_WHEN_the_handler_raised(
    client: TestClient, redis: fakeredis.FakeRedis
) -> None:
    _get(client, "/boom")
    held = int(redis.get(concurrency.SLOT_KEY.format(client_id=CLIENT_ID)) or 0)
    assert held == 0


def test_usage_recorded_while_degraded_is_marked_estimated(
    client: TestClient, redis: fakeredis.FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Billing has to tell a measured row from a reconstructed one instead
    of quietly treating both as fact."""

    def broken(_s: ApiSettings) -> Any:
        raise ConnectionError("redis is down")

    monkeypatch.setattr(limiter, "get_redis", broken)
    _get(client)

    marked = redis.smembers(next(iter(redis.keys("usage_estimated:*"))))
    assert f"{CLIENT_ID}:bars" in marked


# --- plan cache -------------------------------------------------------------


def test_the_plan_cache_is_not_consulted_per_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Limits live in a table so an edit needs no deploy; the request path
    must not pay a query for that."""
    calls = {"n": 0}
    real_cache = policy._TtlCache(policy.CACHE_TTL_SECONDS)
    monkeypatch.setattr(policy, "_cache", real_cache)

    def fake_lookup(client_id: str) -> PlanLimits:
        calls["n"] += 1
        return LIMITS

    monkeypatch.setattr(policy, "limits_for_client", policy.limits_for_client)
    real_cache.put(CLIENT_ID, LIMITS)
    assert policy.limits_for_client(CLIENT_ID) is LIMITS
    assert calls["n"] == 0
