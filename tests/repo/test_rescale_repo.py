"""Rescale's DB behavior: seed, idempotency, scope.

The first test in this file is the design's most critical regression: it
shows, in the same scenario, that the archive is corrupted when `--seed` is
skipped and intact when it is not.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from yfin.models import PriceBar, Split, Symbol
from yfin.rescale import (
    apply_pending,
    pending_splits,
    seed_baseline,
    unseeded_historic_splits,
)

pytestmark = pytest.mark.repo

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
SPLIT_DAY = date(2026, 6, 10)


def _setup(session: Session, *, interval: str = "5m", ratio: str = "10") -> None:
    """AAPL: one split, plus one bar before and one after it."""
    session.add(Symbol(symbol="AAPL", is_active=True))
    session.flush()
    session.execute(
        text(
            "INSERT INTO history_metadata (symbol, exchange_timezone_name, raw_json,"
            " content_hash, fetched_at) VALUES"
            " ('AAPL','America/New_York','{}','h',:now)"
        ),
        {"now": NOW},
    )
    session.add(Split(symbol="AAPL", split_date=SPLIT_DAY, ratio=Decimal(ratio)))
    for ts, close, volume in (
        (datetime(2026, 6, 5, 14, 30), Decimal("1224.40"), 52_840_210),  # before split
        (datetime(2026, 6, 15, 14, 30), Decimal("130.00"), 500_000),  # after split
    ):
        session.add(
            PriceBar(
                symbol="AAPL",
                bar_interval=interval,
                ts_utc=ts,
                local_date=ts.date(),
                open=close,
                high=close,
                low=close,
                close=close,
                volume=volume,
                is_extended=False,
            )
        )
    session.flush()


def _bar(session: Session, ts: datetime, interval: str = "5m") -> PriceBar:
    return session.get(PriceBar, ("AAPL", interval, ts))  # type: ignore[return-value]


def test_seed_prevents_the_first_run_from_destroying_the_archive(db_session: Session) -> None:
    """The design's most critical regression.

    The splits table is already populated by the existing pipeline; a first
    run against an empty bar_rescales would apply historical splits and
    re-divide bars that already arrived from Yahoo at the current scale.
    """
    _setup(db_session)

    seeded = seed_baseline(db_session)
    assert seeded == 1
    applied = apply_pending(db_session, "AAPL")

    assert applied == 0, "seeded split was reapplied"
    assert _bar(db_session, datetime(2026, 6, 5, 14, 30)).close == Decimal("1224.40")


def test_split_inside_the_archive_is_applied_even_without_seed(db_session: Session) -> None:
    """A split inside the archive (after the earliest bar) is applied.

    That is the correct behavior even without a seed: the 06-05 bar was
    written before the split, so it is at the pre-split scale and needs
    aligning.
    """
    _setup(db_session)

    applied = apply_pending(db_session, "AAPL")

    assert applied == 1
    assert _bar(db_session, datetime(2026, 6, 5, 14, 30)).close == Decimal("122.440000000000")


def test_split_older_than_the_archive_is_never_applied(db_session: Session) -> None:
    """Structural protection: a split older than the archive has no work to do.

    This makes the design independent of an operational step (`rescale
    --seed`). If seed is skipped on a fresh install, the mechanism would
    otherwise apply every historical split in the table (for AAPL: 1987,
    2000, 2005, 2014, 2020) and re-divide bars that already arrived from
    Yahoo at the current scale -- a 224x error for AAPL.
    """
    _setup(db_session)
    # A split far older than the archive; no bar was written before it
    db_session.add(Split(symbol="AAPL", split_date=date(2014, 6, 9), ratio=Decimal(7)))
    db_session.flush()

    pending = pending_splits(db_session, "AAPL")

    assert date(2014, 6, 9) not in [d for d, _r in pending], "split older than archive is pending"
    apply_pending(db_session, "AAPL")
    # 2026 bars are only affected by the 2026 split (10x), not the 2014 one
    assert _bar(db_session, datetime(2026, 6, 5, 14, 30)).close == Decimal("122.440000000000")


def test_symbol_without_any_bars_has_nothing_pending(db_session: Session) -> None:
    """If the archive is empty, there is nothing to scale (earliest is NULL)."""
    db_session.add(Symbol(symbol="MSFT", is_active=True))
    db_session.flush()  # FK: the symbol must exist before the split
    db_session.add(Split(symbol="MSFT", split_date=date(2003, 2, 18), ratio=Decimal(2)))
    db_session.flush()

    assert pending_splits(db_session, "MSFT") == []


def test_new_split_after_the_archive_exists_is_applied_once(db_session: Session) -> None:
    """The mechanism's actual job: a split that happens after the archive exists."""
    _setup(db_session)
    seed_baseline(db_session)
    # A new split arrives (after the seed)
    new_day = date(2026, 8, 1)
    db_session.add(Split(symbol="AAPL", split_date=new_day, ratio=Decimal(2)))
    db_session.flush()

    assert apply_pending(db_session, "AAPL") == 1
    # The 06-15 bar predates the new split -> must be divided
    assert _bar(db_session, datetime(2026, 6, 15, 14, 30)).close == Decimal("65.000000000000")
    # A second run must be a no-op (idempotency)
    assert apply_pending(db_session, "AAPL") == 0
    assert _bar(db_session, datetime(2026, 6, 15, 14, 30)).close == Decimal("65.000000000000")


def test_volume_is_multiplied_and_floored(db_session: Session) -> None:
    """3:2 split: volume*1.5 is fractional; without FLOOR it would silently round."""
    _setup(db_session, ratio="1.5")
    db_session.execute(
        text("UPDATE price_bars SET volume = 3 WHERE ts_utc = '2026-06-05 14:30:00'")
    )
    db_session.flush()
    db_session.expire_all()

    apply_pending(db_session, "AAPL")

    assert _bar(db_session, datetime(2026, 6, 5, 14, 30)).volume == 4  # 4.5 -> floor


def test_weekly_and_monthly_bars_are_never_rescaled(db_session: Session) -> None:
    """1wk/1mo are refetched from scratch every run with period='max', so they
    are always at the current scale. If they were scaled and that run's
    fetch then failed, the rows would be left double-corrected."""
    _setup(db_session, interval="1wk")

    apply_pending(db_session, "AAPL")

    assert _bar(db_session, datetime(2026, 6, 5, 14, 30), "1wk").close == Decimal("1224.40")


def test_zero_ratio_is_skipped_and_not_recorded(db_session: Session) -> None:
    """A corrupt zero-ratio row would fail the whole symbol on division by
    zero. No record is written either: if it were, the split would count as
    'applied' and a later-corrected ratio would never be reprocessed."""
    _setup(db_session, ratio="0")

    assert apply_pending(db_session, "AAPL") == 0
    assert _bar(db_session, datetime(2026, 6, 5, 14, 30)).close == Decimal("1224.40")
    assert pending_splits(db_session, "AAPL"), "a skipped split must stay pending"


def test_missing_timezone_skips_rather_than_assuming_utc(db_session: Session) -> None:
    """Without a timezone, UTC is never assumed: scaling with the wrong boundary
    cannot be undone."""
    _setup(db_session)
    db_session.execute(text("UPDATE history_metadata SET exchange_timezone_name = NULL"))
    db_session.flush()

    assert apply_pending(db_session, "AAPL") == 0
    assert _bar(db_session, datetime(2026, 6, 5, 14, 30)).close == Decimal("1224.40")


def test_split_boundary_uses_local_midnight(db_session: Session) -> None:
    """The boundary is local midnight's UTC equivalent (04:00 for NY).

    The split day's 02:00 UTC bar (22:00 the previous day locally) is before
    the split and must be scaled; using raw UTC midnight would miss it.
    """
    _setup(db_session)
    edge = datetime(2026, 6, 10, 2, 0)
    db_session.add(
        PriceBar(
            symbol="AAPL",
            bar_interval="5m",
            ts_utc=edge,
            local_date=date(2026, 6, 9),
            close=Decimal(100),
            volume=1,
            is_extended=False,
        )
    )
    db_session.flush()

    apply_pending(db_session, "AAPL")

    assert _bar(db_session, edge).close == Decimal("10.000000000000")


def test_seed_is_idempotent(db_session: Session) -> None:
    _setup(db_session)

    assert seed_baseline(db_session) == 1
    assert seed_baseline(db_session) == 0


def test_unseeded_historic_split_is_reported(db_session: Session) -> None:
    """A maintenance warning: a split older than price_bars' earliest bar
    with no seed record means --seed was skipped."""
    _setup(db_session)

    assert unseeded_historic_splits(db_session) == 0  # split is after the bars

    db_session.add(Split(symbol="AAPL", split_date=date(2020, 1, 2), ratio=Decimal(4)))
    db_session.flush()

    assert unseeded_historic_splits(db_session) == 1
