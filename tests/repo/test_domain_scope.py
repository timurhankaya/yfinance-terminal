"""replace_scope kapsami: iki dataset AYNI tabloya yaziyor (SI S9.3, S5.11)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from domain_support import run_dataset, run_taxonomy

pytestmark = pytest.mark.repo


def _keys(session: Session) -> set[str]:
    return set(
        session.execute(text("SELECT DISTINCT domain_key FROM domain_top_companies")).scalars()
    )


def test_industry_rankings_do_not_delete_sector_rows(db_session: Session) -> None:
    """Kapsam `domain_key` icermeseydi SEKTOR satirlari silinirdi."""
    run_taxonomy(db_session)
    run_dataset(db_session, "sector_rankings", "sector", "technology")
    assert _keys(db_session) == {"technology"}

    run_dataset(db_session, "industry_rankings", "industry", "semiconductors")
    assert _keys(db_session) == {"technology", "semiconductors"}

    # Sektoru yeniden yazmak endustriyi de dusurmemeli
    run_dataset(db_session, "sector_rankings", "sector", "technology")
    assert _keys(db_session) == {"technology", "semiconductors"}


def test_replace_scope_removes_a_company_that_left_the_list(db_session: Session) -> None:
    """Yahoo listeden bir sirket cikardiginda ESKI SATIR KALMAMALI."""
    import copy

    from domain_support import AS_OF, NOW
    from helpers import domain_data
    from yfin.datasets.domain.payloads import DomainPayload
    from yfin.datasets.registry import DOMAIN_DATASETS
    from yfin.persistence import PostgresRowWriter

    run_taxonomy(db_session)
    run_dataset(db_session, "sector_rankings", "sector", "technology")
    before = int(
        db_session.execute(
            text("SELECT COUNT(*) FROM domain_top_companies WHERE domain_key = 'technology'")
        ).scalar_one()
    )

    shrunk = copy.deepcopy(domain_data("sector", "technology"))
    dropped = shrunk["topCompanies"].pop()["symbol"]
    dataset = DOMAIN_DATASETS["sector_rankings"]
    payload = DomainPayload(
        data=shrunk, fetched_at=NOW, as_of_date=AS_OF, region="US", domain_type="sector"
    )
    dataset.upsert(PostgresRowWriter(db_session), dataset.normalize(payload, "technology"))

    after = int(
        db_session.execute(
            text("SELECT COUNT(*) FROM domain_top_companies WHERE domain_key = 'technology'")
        ).scalar_one()
    )
    assert after == before - 1
    assert (
        db_session.execute(
            text(
                "SELECT COUNT(*) FROM domain_top_companies "
                "WHERE domain_key = 'technology' AND symbol = :s"
            ),
            {"s": dropped},
        ).scalar_one()
        == 0
    )


def test_regions_do_not_delete_each_other(db_session: Session) -> None:
    run_taxonomy(db_session)
    run_dataset(db_session, "sector_rankings", "sector", "technology", region="US")
    us = int(
        db_session.execute(
            text("SELECT COUNT(*) FROM domain_top_companies WHERE region = 'US'")
        ).scalar_one()
    )
    run_dataset(db_session, "sector_rankings", "sector", "technology", region="GB")
    assert (
        int(
            db_session.execute(
                text("SELECT COUNT(*) FROM domain_top_companies WHERE region = 'US'")
            ).scalar_one()
        )
        == us
    )
