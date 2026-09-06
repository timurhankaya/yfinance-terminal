"""Live integration tests for price_bars.

`-m live`: real Yahoo API + real PostgreSQL. Fixture tests verify a
recorded past shape of the source; these verify its current shape and catch
BAR_LIMITS drift early.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from yfin.client import make_ticker
from yfin.datasets.bars import BAR_LIMITS, IntervalBarDataset, plan_windows
from yfin.datasets.base import SyncContext
from yfin.persistence import PostgresRowWriter

pytestmark = pytest.mark.live


def _ctx(symbol: str) -> SyncContext:
    return SyncContext(
        symbol,
        make_ticker(symbol),
        datetime.now(UTC),
    )


@pytest.mark.parametrize("symbol", ["AAPL", "THYAO.IS"])
def test_end_to_end_5m_write(db_session: Session, symbol: str) -> None:
    """fetch -> normalize -> write -> verify."""
    # ON CONFLICT DO NOTHING is required: live tests share one schema, and an
    # earlier module (test_live_analysis) may have already committed this
    # symbol. A plain INSERT would then fail on a duplicate key -- passing
    # alone but breaking when run together.
    db_session.execute(
        text(
            "INSERT INTO symbols (symbol, is_active, unknown_streak, created_at, updated_at) "
            "VALUES (:s, true, 0, now(), now()) ON CONFLICT (symbol) DO NOTHING"
        ),
        {"s": symbol},
    )
    db_session.flush()

    dataset = IntervalBarDataset("5m")
    payload = dataset.fetch(_ctx(symbol))
    result = dataset.normalize(payload, symbol)
    stats = dataset.upsert(PostgresRowWriter(db_session), result)

    assert stats.attempted["price_bars"] > 0
    assert stats.verified["price_bars"] == stats.attempted["price_bars"]


def test_first_fill_1m_slices_stay_inside_the_request_limit() -> None:
    """The planner's slices must not error at the real limit.

    This test exists because of a measured bug: BAR_LIMITS["1m"] was
    previously (8, 30), and since the first slice started right at the
    limit, live runs raised YFPricesMissingError. This breaks first if
    Yahoo's limits change.
    """
    now = datetime.now(UTC)
    plan = plan_windows("1m", None, now)
    ticker = make_ticker("MSFT")

    oldest = plan.windows[0]
    frame = ticker.history(
        interval="1m", start=oldest[0].isoformat(), end=oldest[1].isoformat(), prepost=True
    )

    assert not frame.empty, f"oldest slice ({oldest[0]} -> {oldest[1]}) was rejected"


@pytest.mark.parametrize("interval", ["5m", "60m"])
def test_declared_depth_is_still_accepted_by_yahoo(interval: str) -> None:
    """Are the depth values in BAR_LIMITS still accepted by Yahoo.

    If Yahoo ever narrows the window, this test breaks before data starts
    silently disappearing.
    """
    _per_request, depth = BAR_LIMITS[interval]
    assert depth is not None
    now = datetime.now(UTC)
    plan = plan_windows(interval, None, now)
    oldest = plan.windows[0]

    frame = make_ticker("MSFT").history(
        interval=interval, start=oldest[0].isoformat(), end=oldest[1].isoformat()
    )

    assert not frame.empty


def test_weekly_uses_period_max_and_returns_monday_anchored_bars() -> None:
    """1wk's first fill is unsliced (period='max') and bars are Monday-anchored."""
    now = datetime.now(UTC)
    plan = plan_windows("1wk", None, now)
    assert plan.windows == ()

    payload = IntervalBarDataset("1wk").fetch(_ctx("MSFT"))

    assert not payload.frame.empty
    assert all(ts.weekday() == 0 for ts in payload.frame.index[:20])


def test_extended_flag_is_set_for_a_us_symbol() -> None:
    """A prepost fetch of AAPL must produce an off-session bar; if not, either
    prepost was turned off or tradingPeriods' shape changed."""
    dataset = IntervalBarDataset("5m")
    payload = dataset.fetch(_ctx("AAPL"))
    rows = dataset.normalize(payload, "AAPL").writes[0].rows

    assert any(r["is_extended"] for r in rows)
    assert any(not r["is_extended"] for r in rows)
