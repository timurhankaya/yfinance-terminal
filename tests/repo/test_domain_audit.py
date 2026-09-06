"""`yfin domain audit` -- uc bagimsiz kontrol (SI S9.3, S8.3)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from domain_support import AS_OF, FIXTURE_SECTORS, run_dataset, run_taxonomy
from yfin.domain_audit import audit_domains, expected_cell_count

pytestmark = pytest.mark.repo


def _seed(session: Session) -> None:
    run_taxonomy(session)
    for key in FIXTURE_SECTORS:
        run_dataset(session, "sector_profile", "sector", key)


def test_audit_passes_on_a_consistent_taxonomy(db_session: Session) -> None:
    _seed(db_session)
    report = audit_domains(db_session, as_of=AS_OF)
    assert report.expected_industries == report.industry_count
    # Fixture evreni 11 sektorun tamami degil; sektor sayisi kontrolu bunu
    # dogru sekilde SORUN olarak bildirir.
    assert report.sector_count == len(FIXTURE_SECTORS)
    assert any("sektor sayisi" in p for p in report.problems)
    assert not any("endustri sayisi" in p for p in report.problems)


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
    assert any("endustri sayisi" in p for p in report.problems)


def test_audit_uses_the_latest_row_not_an_exact_day_match(db_session: Session) -> None:
    """KAPI hash'i esitse o gun HIC `domain_metrics` satiri yazilmaz.

    Kesin esitlik kullanilsaydi audit SAHTE BASARISIZLIK verirdi.
    """
    _seed(db_session)
    later = AS_OF + timedelta(days=3)
    report = audit_domains(db_session, as_of=later)
    assert report.expected_industries == report.industry_count


def test_audit_reports_failed_cells_of_a_run(db_session: Session) -> None:
    _seed(db_session)
    # `LAST_INSERT_ID()` PostgreSQL'de YOKTUR; uretilen anahtar
    # `RETURNING` ile ayni ifadeden alinir -- ustelik yarissiz.
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


def test_expected_cell_count_formula() -> None:
    assert expected_cell_count(1) == 1239
    assert expected_cell_count(1, industry_count=0) == 2 + 44 + 33
