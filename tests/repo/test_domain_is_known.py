"""`is_known` hash GOVDESINDE: evren degisince kapi acilir (SI S9.3, S7.4)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from domain_support import NOW, run_dataset, run_taxonomy
from helpers import domain_data

pytestmark = pytest.mark.repo

LATER = NOW + timedelta(hours=6)


def _known_flags(session: Session, key: str) -> dict[str, bool]:
    return {
        row.symbol: bool(row.is_known)
        for row in session.execute(
            text(
                "SELECT symbol, is_known FROM domain_top_companies WHERE domain_key = :k"
            ),
            {"k": key},
        )
    }


def test_unknown_symbols_start_at_zero(db_session: Session) -> None:
    run_taxonomy(db_session)
    run_dataset(db_session, "sector_rankings", "sector", "technology")
    flags = _known_flags(db_session, "technology")
    assert flags
    assert not any(flags.values()), "evrende olmayan semboller is_known=0 olmali"


def test_adding_the_symbol_reopens_the_gate_and_flips_the_flag(
    db_session: Session,
) -> None:
    """DISLANSAYDI bayrak 0'da DONUP KALIRDI: kapi `skipped` derdi."""
    run_taxonomy(db_session)
    run_dataset(db_session, "sector_rankings", "sector", "technology")
    target = domain_data("sector", "technology")["topCompanies"][0]["symbol"]

    db_session.execute(
        text(
            "INSERT INTO symbols (symbol, is_active, unknown_streak, created_at, updated_at) "
            "VALUES (:s, 1, 0, :t, :t)"
        ),
        {"s": target, "t": NOW},
    )

    stats = run_dataset(
        db_session, "sector_rankings", "sector", "technology", fetched_at=LATER
    )
    assert stats.attempted.get("domain_top_companies"), "kapi ACILMALIYDI"
    assert _known_flags(db_session, "technology")[target] is True


def test_non_ticker_fund_ids_stay_unknown(db_session: Session) -> None:
    """`0P0001WO1I` bir Morningstar kimligi; FK olsaydi TUM TUR dusurdu."""
    run_taxonomy(db_session)
    run_dataset(db_session, "sector_rankings", "sector", "healthcare")
    rows = {
        row.symbol: bool(row.is_known)
        for row in db_session.execute(
            text("SELECT symbol, is_known FROM domain_top_funds WHERE domain_key = 'healthcare'")
        )
    }
    morningstar = [s for s in rows if s.startswith("0P")]
    assert morningstar
    assert all(rows[s] is False for s in morningstar)
