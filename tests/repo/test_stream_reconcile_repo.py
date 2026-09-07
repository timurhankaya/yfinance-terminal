"""Closing bar_gaps from ticks, against a real database.

Every test here corresponds to a way this could quietly corrupt the bar
archive: overwriting real bars, inventing volume, filing a bar under the
wrong calendar day, or skipping the very gaps it exists to fill.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from yfin.models.bars import GAP_FETCH_FAILED, GAP_RETENTION_EXPIRED, RESOLVED_BY_TICKS
from yfin.stream.reconcile import GapReconciler, local_date_for
from yfin.stream.writer import TICK_COLUMNS

pytestmark = pytest.mark.repo

# 09:31 New York on a weekday, expressed in UTC.
GAP_START = datetime(2026, 9, 7, 13, 30, tzinfo=UTC)
GAP_END = GAP_START + timedelta(minutes=10)


@pytest.fixture
def factory(db_session: Session) -> sessionmaker[Session]:
    return sessionmaker(
        bind=db_session.connection(),
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )


@pytest.fixture
def reconciler(factory: sessionmaker[Session]) -> GapReconciler:
    return GapReconciler(factory)


def _symbol(session: Session, symbol: str = "AAPL", tz: str | None = "America/New_York") -> None:
    session.execute(
        text(
            "INSERT INTO symbols (symbol, timezone, is_active, unknown_streak, "
            "                     created_at, updated_at) "
            "VALUES (:s, :tz, true, 0, now(), now()) "
            "ON CONFLICT (symbol) DO UPDATE SET timezone = :tz"
        ),
        {"s": symbol, "tz": tz},
    )


def _gap(
    session: Session,
    *,
    symbol: str = "AAPL",
    reason: str = GAP_RETENTION_EXPIRED,
    start: datetime = GAP_START,
    end: datetime = GAP_END,
) -> None:
    session.execute(
        text(
            "INSERT INTO bar_gaps (symbol, bar_interval, gap_start_utc, gap_end_utc, "
            "                      detected_at, reason) "
            "VALUES (:s, '1m', :start, :end, now(), :r)"
        ),
        {"s": symbol, "start": start, "end": end, "r": reason},
    )


def _tick(
    session: Session,
    ts: datetime,
    price: str,
    *,
    symbol: str = "AAPL",
    market_hours: int = 1,
) -> None:
    row = dict.fromkeys(TICK_COLUMNS)
    row.update(
        {
            "symbol": symbol,
            "ts_utc": ts,
            "payload_hash": f"{int(ts.timestamp()):016x}"[-16:],
            "received_at": ts,
            "price": Decimal(price) if price is not None else None,
            "quote_type_code": 8,
            "market_hours_code": market_hours,
        }
    )
    columns = ", ".join(TICK_COLUMNS)
    binds = ", ".join(f":{c}" for c in TICK_COLUMNS)
    session.execute(
        text(f"INSERT INTO live_ticks ({columns}) VALUES ({binds}) ON CONFLICT DO NOTHING"),
        row,
    )


def _bars(session: Session, symbol: str = "AAPL") -> list[tuple[object, ...]]:
    return [
        tuple(r)
        for r in session.execute(
            text(
                "SELECT ts_utc, open, high, low, close, volume, is_extended, local_date "
                "  FROM price_bars WHERE symbol = :s AND bar_interval = '1m' "
                " ORDER BY ts_utc"
            ),
            {"s": symbol},
        ).all()
    ]


# --- the gaps it must not skip ---------------------------------------------


def test_retention_expired_gaps_are_filled(
    reconciler: GapReconciler, db_session: Session
) -> None:
    """These are the reason the module exists.

    Yahoo drops 1m data after 29 days, so a retention_expired window can
    never be fetched again -- the tick archive is the only thing left that
    knows what happened. An earlier draft filtered them out, which meant
    the reconciliation skipped exactly the gaps that needed it.
    """
    _symbol(db_session)
    _gap(db_session, reason=GAP_RETENTION_EXPIRED)
    _tick(db_session, GAP_START + timedelta(seconds=5), "100.5")
    db_session.commit()

    stats = reconciler.run()
    assert stats.gaps_closed == 1
    assert stats.bars_written == 1


def test_fetch_failed_gaps_are_filled_too(
    reconciler: GapReconciler, db_session: Session
) -> None:
    _symbol(db_session)
    _gap(db_session, reason=GAP_FETCH_FAILED)
    _tick(db_session, GAP_START + timedelta(seconds=5), "100.5")
    db_session.commit()
    assert reconciler.run().gaps_closed == 1


def test_a_closed_gap_records_what_closed_it(
    reconciler: GapReconciler, db_session: Session
) -> None:
    """A separate column, because `reason` is inside the gap write's
    update_columns and the next detection would overwrite it."""
    _symbol(db_session)
    _gap(db_session)
    _tick(db_session, GAP_START, "100.5")
    db_session.commit()
    reconciler.run()

    row = db_session.execute(
        text("SELECT resolved_at, resolved_by, reason FROM bar_gaps")
    ).one()
    assert row[0] is not None
    assert row[1] == RESOLVED_BY_TICKS
    assert row[2] == GAP_RETENTION_EXPIRED  # the original reason survives


def test_an_already_resolved_gap_is_left_alone(
    reconciler: GapReconciler, db_session: Session
) -> None:
    _symbol(db_session)
    _gap(db_session)
    db_session.execute(text("UPDATE bar_gaps SET resolved_at = now()"))
    _tick(db_session, GAP_START, "100.5")
    db_session.commit()
    assert reconciler.run().gaps_examined == 0


# --- what it must never overwrite ------------------------------------------


def test_existing_bars_are_never_touched(
    reconciler: GapReconciler, db_session: Session
) -> None:
    """A fetch_failed gap is recorded at day granularity, so it can span
    days and contain real bars. A derived bar has no volume -- writing
    over one would replace a correct volume with NULL.
    """
    _symbol(db_session)
    _gap(db_session, reason=GAP_FETCH_FAILED)
    minute = GAP_START + timedelta(minutes=1)
    db_session.execute(
        text(
            "INSERT INTO price_bars (symbol, bar_interval, ts_utc, local_date, "
            "                        open, high, low, close, volume, is_extended) "
            "VALUES ('AAPL','1m',:ts,:d,10,11,9,10.5,4242,false)"
        ),
        {"ts": minute, "d": date(2026, 9, 7)},
    )
    _tick(db_session, minute + timedelta(seconds=5), "999")
    _tick(db_session, GAP_START + timedelta(seconds=5), "100.5")
    db_session.commit()

    stats = reconciler.run()
    assert stats.minutes_skipped_existing == 1

    stored = {row[0]: row for row in _bars(db_session)}
    assert stored[minute][5] == 4242  # volume intact
    assert stored[minute][4] == Decimal("10.5")  # close intact


# --- what it must never invent ---------------------------------------------


def test_derived_bars_have_no_volume(
    reconciler: GapReconciler, db_session: Session
) -> None:
    """day_volume is cumulative and the feed is a ~1s snapshot, so a
    per-minute figure would be a guess. NULL says "unknown", which is
    true."""
    _symbol(db_session)
    _gap(db_session)
    _tick(db_session, GAP_START, "100")
    db_session.commit()
    reconciler.run()
    assert _bars(db_session)[0][5] is None


def test_a_minute_with_no_priced_tick_is_skipped(
    reconciler: GapReconciler, db_session: Session
) -> None:
    """price_bars.close is NOT NULL, and a tick may legitimately carry
    only bid/ask."""
    _symbol(db_session)
    _gap(db_session)
    _tick(db_session, GAP_START, None)  # type: ignore[arg-type]
    db_session.commit()
    assert reconciler.run().bars_written == 0


# --- the calendar day ------------------------------------------------------


def test_local_date_uses_the_exchange_zone_not_utc() -> None:
    """Deriving it from UTC lands a day early for positive-offset
    exchanges -- the exact trap normalize.to_local_date documents."""
    istanbul_midnight = datetime(2026, 5, 10, 21, 0, tzinfo=UTC)  # 00:00 +03
    assert local_date_for(istanbul_midnight, "Europe/Istanbul") == date(2026, 5, 11)
    assert istanbul_midnight.date() == date(2026, 5, 10)  # what UTC would have given


def test_bars_carry_the_exchange_local_date(
    reconciler: GapReconciler, db_session: Session
) -> None:
    _symbol(db_session, tz="America/New_York")
    _gap(db_session)
    _tick(db_session, GAP_START, "100")
    db_session.commit()
    reconciler.run()
    assert _bars(db_session)[0][7] == date(2026, 9, 7)


def test_a_symbol_without_a_timezone_is_refused(
    reconciler: GapReconciler, db_session: Session
) -> None:
    """local_date is NOT NULL and a wrong one files the bar under the
    wrong day, so the gap stays open and is reported instead."""
    _symbol(db_session, tz=None)
    _gap(db_session)
    _tick(db_session, GAP_START, "100")
    db_session.commit()

    stats = reconciler.run()
    assert stats.gaps_closed == 0
    assert stats.gaps_without_timezone == ["AAPL"]
    assert db_session.execute(
        text("SELECT resolved_at FROM bar_gaps")
    ).scalar_one() is None


# --- OHLC ------------------------------------------------------------------


def test_ohlc_comes_from_the_ticks_in_the_minute(
    reconciler: GapReconciler, db_session: Session
) -> None:
    _symbol(db_session)
    _gap(db_session)
    for offset, price in [(1, "100"), (10, "105"), (20, "98"), (30, "102")]:
        _tick(db_session, GAP_START + timedelta(seconds=offset), price)
    db_session.commit()
    reconciler.run()

    bar = _bars(db_session)[0]
    assert bar[1] == Decimal("100")  # open, first
    assert bar[2] == Decimal("105")  # high
    assert bar[3] == Decimal("98")  # low
    assert bar[4] == Decimal("102")  # close, last


def test_each_minute_becomes_its_own_bar(
    reconciler: GapReconciler, db_session: Session
) -> None:
    """A gap row can cover days, so the window is walked minute by minute
    rather than collapsed into one bar."""
    _symbol(db_session)
    _gap(db_session)
    for minute in range(3):
        _tick(db_session, GAP_START + timedelta(minutes=minute, seconds=5), f"10{minute}")
    db_session.commit()
    reconciler.run()
    assert len(_bars(db_session)) == 3


def test_extended_session_is_derived_from_market_hours(
    reconciler: GapReconciler, db_session: Session
) -> None:
    """PRE_MARKET is code 0, which is why that column is NOT NULL and
    exempt from the presence rule."""
    _symbol(db_session)
    _gap(db_session)
    _tick(db_session, GAP_START, "100", market_hours=0)  # PRE_MARKET
    db_session.commit()
    reconciler.run()
    assert _bars(db_session)[0][6] is True


# --- dry run ---------------------------------------------------------------


def test_dry_run_writes_nothing(reconciler: GapReconciler, db_session: Session) -> None:
    _symbol(db_session)
    _gap(db_session)
    _tick(db_session, GAP_START, "100")
    db_session.commit()

    stats = reconciler.run(dry_run=True)
    assert stats.bars_written == 1
    assert _bars(db_session) == []
    assert db_session.execute(
        text("SELECT resolved_at FROM bar_gaps")
    ).scalar_one() is None


def test_a_gap_with_no_ticks_stays_open(
    reconciler: GapReconciler, db_session: Session
) -> None:
    """Nothing to fill it with, so it must remain visible as a gap."""
    _symbol(db_session)
    _gap(db_session)
    db_session.commit()
    stats = reconciler.run()
    assert stats.gaps_closed == 0
    assert db_session.execute(
        text("SELECT resolved_at FROM bar_gaps")
    ).scalar_one() is None
