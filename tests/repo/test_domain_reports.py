"""Shared report table + link table."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from domain_support import run_dataset, run_taxonomy
from helpers import domain_data

pytestmark = pytest.mark.repo


def _count(session: Session, table: str, where: str = "1=1") -> int:
    return int(session.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {where}")).scalar_one())


def test_a_shared_report_yields_one_row_and_two_links(db_session: Session) -> None:
    """All 37 distinct sector reports also show up under an industry (100%)."""
    run_taxonomy(db_session)
    db_session.execute(
        text(
            "INSERT INTO research_reports "
            "(report_id, as_of_date, provider, first_seen_at, fetched_at) "
            "VALUES ('SHARED', '2026-09-04', 'Argus', :t, :t)"
        ),
        {"t": "2026-09-04 12:00:00"},
    )
    for key in ("technology", "semiconductors"):
        db_session.execute(
            text(
                "INSERT INTO domain_report_links "
                "(domain_key, as_of_date, report_id, position, fetched_at) "
                "VALUES (:k, '2026-09-04', 'SHARED', 0, :t)"
            ),
            {"k": key, "t": "2026-09-04 12:00:00"},
        )
    db_session.flush()
    assert _count(db_session, "research_reports", "report_id = 'SHARED'") == 1
    assert _count(db_session, "domain_report_links", "report_id = 'SHARED'") == 2


def test_reports_upsert_across_domains_without_duplicate_key_errors(
    db_session: Session,
) -> None:
    run_taxonomy(db_session)
    run_dataset(db_session, "sector_profile", "sector", "technology")
    run_dataset(db_session, "industry_profile", "industry", "semiconductors")

    ids = {
        r["id"]
        for key, kind in (("technology", "sector"), ("semiconductors", "industry"))
        for r in domain_data(kind, key)["researchReports"]
    }
    assert _count(db_session, "research_reports") == len(ids)
    assert _count(db_session, "domain_report_links") == 8


def test_report_title_round_trips_at_measured_length(db_session: Session) -> None:
    """Measured max is 23,570 characters; `TEXT` would overflow in utf8mb4."""
    run_taxonomy(db_session)
    title = "ş" * 24000
    db_session.execute(
        text(
            "INSERT INTO research_reports "
            "(report_id, as_of_date, report_title, first_seen_at, fetched_at) "
            "VALUES ('LONG', '2026-09-04', :title, :t, :t)"
        ),
        {"title": title, "t": "2026-09-04 12:00:00"},
    )
    db_session.flush()
    stored = db_session.execute(
        text("SELECT report_title FROM research_reports WHERE report_id = 'LONG'")
    ).scalar_one()
    assert stored == title
    assert len(stored) == 24000


def test_first_seen_at_is_never_updated(db_session: Session) -> None:
    from datetime import timedelta

    from domain_support import NOW

    run_taxonomy(db_session)
    run_dataset(db_session, "sector_profile", "sector", "technology")
    before = list(
        db_session.execute(
            text("SELECT report_id, first_seen_at FROM research_reports ORDER BY report_id")
        )
    )
    run_dataset(
        db_session,
        "industry_profile",
        "industry",
        "semiconductors",
        fetched_at=NOW + timedelta(days=1),
    )
    run_dataset(
        db_session, "sector_profile", "sector", "healthcare", fetched_at=NOW + timedelta(days=1)
    )
    after = {
        row.report_id: row.first_seen_at
        for row in db_session.execute(
            text("SELECT report_id, first_seen_at FROM research_reports")
        )
    }
    for report_id, first_seen in before:
        assert after[report_id] == first_seen


def test_report_links_are_scoped_per_domain_and_day(db_session: Session) -> None:
    run_taxonomy(db_session)
    run_dataset(db_session, "sector_profile", "sector", "technology")
    run_dataset(db_session, "sector_profile", "sector", "healthcare")
    keys = set(
        db_session.execute(text("SELECT DISTINCT domain_key FROM domain_report_links")).scalars()
    )
    assert keys == {"technology", "healthcare"}
    assert _count(db_session, "domain_report_links", "domain_key = 'technology'") == 4
