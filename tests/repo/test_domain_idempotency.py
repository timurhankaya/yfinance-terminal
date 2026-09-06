"""Ayni fixture iki kez: kapinin GERCEK MySQL uzerindeki davranisi (SI S9.3)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from domain_support import NOW, run_dataset, run_taxonomy
from yfin.datasets.registry import DOMAIN_DATASETS
from yfin.models import ItemStatus
from yfin.runner import _record_items

pytestmark = pytest.mark.repo

LATER = NOW + timedelta(hours=6)


def _statuses(dataset_name: str, stats, region: str) -> dict[str, ItemStatus]:
    records = _record_items(
        DOMAIN_DATASETS[dataset_name], "^YH311", stats, fetched=0, duration_ms=1, region=region
    )
    return {r.table_name: r.status for r in records}


def _count(session: Session, table: str) -> int:
    return int(session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())


def test_second_run_skips_data_tables_but_keeps_the_gate_ok(db_session: Session) -> None:
    run_taxonomy(db_session)
    first = run_dataset(db_session, "sector_rankings", "sector", "technology")
    rows_after_first = _count(db_session, "domain_top_companies")

    second = run_dataset(
        db_session, "sector_rankings", "sector", "technology", fetched_at=LATER
    )
    assert _count(db_session, "domain_top_companies") == rows_after_first

    statuses = _statuses("sector_rankings", second, "US")
    assert statuses["domain_top_companies"] is ItemStatus.SKIPPED
    assert statuses["domain_top_funds"] is ItemStatus.SKIPPED
    # KAPI HUCRESI `ok` KALIR: kapi satiri her iki dalda da yazilir.
    # "tum hucreler skipped" iddiasi YANLIS olurdu.
    assert statuses["domain_asof_state"] is ItemStatus.OK
    assert _statuses("sector_rankings", first, "US")["domain_top_companies"] is ItemStatus.OK


def test_taxonomy_cells_stay_ok_because_it_is_a_plain_upsert(db_session: Session) -> None:
    run_taxonomy(db_session)
    stats = run_taxonomy(db_session, fetched_at=LATER)
    statuses = _statuses("domain_taxonomy", stats, "*")
    assert statuses["symbols"] is ItemStatus.OK
    assert statuses["domains"] is ItemStatus.OK


def test_gate_fetched_at_advances_while_first_seen_at_is_frozen(
    db_session: Session,
) -> None:
    run_taxonomy(db_session)
    run_dataset(db_session, "sector_rankings", "sector", "technology")
    before = db_session.execute(
        text(
            "SELECT first_seen_at, fetched_at, content_hash FROM domain_asof_state "
            "WHERE domain_key = 'technology' AND dataset = 'sector_rankings' AND region = 'US'"
        )
    ).one()

    run_dataset(db_session, "sector_rankings", "sector", "technology", fetched_at=LATER)
    after = db_session.execute(
        text(
            "SELECT first_seen_at, fetched_at, content_hash FROM domain_asof_state "
            "WHERE domain_key = 'technology' AND dataset = 'sector_rankings' AND region = 'US'"
        )
    ).one()

    assert after.first_seen_at == before.first_seen_at
    assert after.fetched_at > before.fetched_at
    assert after.content_hash == before.content_hash


def test_regions_get_independent_gate_rows(db_session: Session) -> None:
    run_taxonomy(db_session)
    run_dataset(db_session, "sector_rankings", "sector", "technology", region="US")
    run_dataset(db_session, "sector_rankings", "sector", "technology", region="GB")
    rows = list(
        db_session.execute(
            text(
                "SELECT region, content_hash FROM domain_asof_state "
                "WHERE domain_key = 'technology' AND dataset = 'sector_rankings' "
                "ORDER BY region"
            )
        )
    )
    assert [r.region for r in rows] == ["GB", "US"]
    assert rows[0].content_hash != rows[1].content_hash


def test_regionless_dataset_writes_the_star_marker(db_session: Session) -> None:
    run_taxonomy(db_session)
    run_dataset(db_session, "sector_profile", "sector", "technology")
    region = db_session.execute(
        text(
            "SELECT region FROM domain_asof_state "
            "WHERE domain_key = 'technology' AND dataset = 'sector_profile'"
        )
    ).scalar_one()
    assert region == "*"


def test_two_profile_datasets_share_tables_but_not_gate_rows(db_session: Session) -> None:
    run_taxonomy(db_session)
    run_dataset(db_session, "sector_profile", "sector", "technology")
    run_dataset(db_session, "industry_profile", "industry", "semiconductors")
    datasets = set(
        db_session.execute(
            text("SELECT DISTINCT dataset FROM domain_asof_state")
        ).scalars()
    )
    assert datasets == {"sector_profile", "industry_profile"}
    keys = set(db_session.execute(text("SELECT DISTINCT domain_key FROM domain_metrics")).scalars())
    assert keys == {"technology", "semiconductors"}
