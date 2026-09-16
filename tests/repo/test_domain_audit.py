"""`yfin domain audit` -- three independent checks."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from domain_support import AS_OF, FIXTURE_SECTORS, run_dataset, run_taxonomy
from yfin.pipeline.domain_audit import audit_domains

pytestmark = pytest.mark.repo


def _seed(session: Session) -> None:
    run_taxonomy(session)
    for key in FIXTURE_SECTORS:
        run_dataset(session, "sector_profile", "sector", key)


def test_audit_passes_on_a_consistent_taxonomy(db_session: Session) -> None:
    _seed(db_session)
    report = audit_domains(db_session, as_of=AS_OF)
    assert report.expected_industries == report.industry_count
    # The fixture universe is not all 11 sectors; the sector-count check
    # correctly flags this as a problem.
    assert report.sector_count == len(FIXTURE_SECTORS)
    assert any("sector count" in p for p in report.problems)
    assert not any("industry count" in p for p in report.problems)


def test_deleting_one_industry_makes_the_audit_fail(db_session: Session) -> None:
    _seed(db_session)
    victim = db_session.execute(
        text(
            "SELECT domain_key FROM domains WHERE domain_type = 'industry' "
            "ORDER BY domain_key LIMIT 1"
        )
    ).scalar_one()
    db_session.execute(text("DELETE FROM domains WHERE domain_key = :k"), {"k": victim})

    report = audit_domains(db_session, as_of=AS_OF)
    assert report.exit_code() == 1
    assert any("industry count" in p for p in report.problems)


def test_audit_uses_the_latest_row_not_an_exact_day_match(db_session: Session) -> None:
    """No `domain_metrics` row is written for a day when the gate hash matches.

    An exact-date match would make the audit fail spuriously on such a day.
    """
    _seed(db_session)
    later = AS_OF + timedelta(days=3)
    report = audit_domains(db_session, as_of=later)
    assert report.expected_industries == report.industry_count


def test_audit_reports_failed_cells_of_a_run(db_session: Session) -> None:
    _seed(db_session)
    # PostgreSQL has no `LAST_INSERT_ID()`; the generated key comes back from
    # the same statement via `RETURNING`, race-free.
    run_id = int(
        db_session.execute(
            text(
                "INSERT INTO sync_runs "
                "(started_at, scope, status, symbol_count, dataset_count) "
                "VALUES (:t, 'domain', 'running', 0, 5) RETURNING id"
            ),
            {"t": "2026-09-04 12:00:00"},
        ).scalar_one()
    )
    db_session.execute(
        text(
            "INSERT INTO sync_run_items "
            "(run_id, symbol, dataset, status, table_name, region) "
            "VALUES (:r, '^YH311', 'sector_rankings', 'failed', 'domain_top_companies', 'GB')"
        ),
        {"r": run_id},
    )
    report = audit_domains(db_session, as_of=AS_OF, run_id=run_id)
    assert report.failed_cells == 1
    assert report.cells_by_status == {"failed": 1}
    assert report.exit_code() == 1
