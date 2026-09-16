"""Generalized `prune_asof` plus orphan report cleanup."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from domain_support import AS_OF, NOW, run_taxonomy
from yfin.datasets.registry import DOMAIN_DATASETS, SYMBOL_DATASETS
from yfin.pipeline.prune import (
    asof_table_datasets,
    prune_asof,
    prune_orphan_reports,
    run_prune,
)

pytestmark = pytest.mark.repo

DOMAIN_TRIPLE = {
    "registry": DOMAIN_DATASETS,
    "gate_table": "domain_asof_state",
    "scope_column": "domain_key",
}


def _count(session: Session, table: str, where: str = "1=1") -> int:
    return int(session.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {where}")).scalar_one())


def test_symbol_side_defaults_are_unchanged() -> None:
    """The symbol-side as-of mapping keeps its defaults."""
    mapping = asof_table_datasets()
    assert "asof_state" not in mapping
    # Datasets are picked up by being as-of, not by being listed anywhere.
    assert len(mapping) == 16
    assert mapping["institutional_holders"] == ["institutional_holders", "mutualfund_holders"]
    # Domain tables do not leak into the symbol side's scope
    assert not any(name.startswith("domain_") for name in mapping)


def test_domain_side_mapping_excludes_the_gate_and_the_shared_report_table() -> None:
    mapping = asof_table_datasets(DOMAIN_DATASETS, "domain_asof_state")
    assert "domain_asof_state" not in mapping
    assert mapping["domain_top_companies"] == ["sector_rankings", "industry_rankings"]
    # `domains` is gone now: it's a static identity table with no `as_of_date`
    # column. `prune_asof` already skipped it (the `"as_of_date" not in
    # table.c` branch); showing it in the map gave the false impression it
    # gets pruned. `asof_table_datasets` moved this filter upfront during the
    # SQ audit.
    assert set(mapping) == {
        "domain_metrics",
        "research_reports",
        "domain_report_links",
        "domain_top_companies",
        "domain_top_funds",
        "domain_top_movers",
    }
    assert "domains" not in mapping


def _seed_two_days(session: Session) -> None:
    """Two days with different content: an identical fixture would match the
    content_hash and the gate would write nothing (`as_of_date` is volatile),
    so the row meant to be pruned would never exist."""
    import copy

    from helpers import domain_data
    from yfin.datasets.domain.payloads import DomainPayload
    from yfin.storage.persistence import PostgresRowWriter

    run_taxonomy(session)
    for offset, (day, moment) in enumerate(
        ((AS_OF, NOW), (AS_OF + timedelta(days=1), NOW + timedelta(days=1)))
    ):
        data = copy.deepcopy(domain_data("sector", "technology"))
        # Mirrors reality: `marketCap` changed in 11/11 sectors within 15 minutes.
        data["overview"]["marketCap"]["raw"] += offset
        data["topCompanies"][0]["lastPrice"]["raw"] += offset
        for name in ("sector_profile", "sector_rankings"):
            dataset = DOMAIN_DATASETS[name]
            payload = DomainPayload(
                data=data,
                fetched_at=moment,
                as_of_date=day,
                region="US",
                domain_type="sector",
            )
            dataset.upsert(
                PostgresRowWriter(session), dataset.normalize(payload, "technology")
            )


def test_domain_prune_keeps_the_latest_day(db_session: Session) -> None:
    _seed_two_days(db_session)
    assert _count(db_session, "domain_metrics") == 2

    removed = prune_asof(db_session, AS_OF + timedelta(days=1), **DOMAIN_TRIPLE)
    assert removed["domain_metrics"] == 1
    days = set(db_session.execute(text("SELECT as_of_date FROM domain_metrics")).scalars())
    assert days == {AS_OF + timedelta(days=1)}


def test_domain_prune_never_touches_the_gate_or_the_identity_table(
    db_session: Session,
) -> None:
    """Deleting the gate row would lose `first_seen_at` and rewrite the entire
    history on the next run."""
    _seed_two_days(db_session)
    gates = _count(db_session, "domain_asof_state")
    domains = _count(db_session, "domains")

    prune_asof(db_session, AS_OF + timedelta(days=90), **DOMAIN_TRIPLE)
    assert _count(db_session, "domain_asof_state") == gates
    assert _count(db_session, "domains") == domains


def test_shared_report_table_is_not_pruned_by_scope(db_session: Session) -> None:
    """`research_reports` has no `domain_key` column -- it cannot be pruned by scope."""
    _seed_two_days(db_session)
    reports = _count(db_session, "research_reports")
    removed = prune_asof(db_session, AS_OF + timedelta(days=90), **DOMAIN_TRIPLE)
    assert "research_reports" not in removed
    assert _count(db_session, "research_reports") == reports


def test_orphan_reports_are_removed_only_after_links_go(db_session: Session) -> None:
    _seed_two_days(db_session)
    assert prune_orphan_reports(db_session, dry_run=True) == 0

    db_session.execute(text("DELETE FROM domain_report_links"))
    orphans = _count(db_session, "research_reports")
    assert prune_orphan_reports(db_session) == orphans
    assert _count(db_session, "research_reports") == 0


def test_symbol_side_pruning_behaviour_is_unchanged(db_session: Session) -> None:
    """The domain triple does not leak into the symbol side."""
    _seed_two_days(db_session)
    removed = prune_asof(db_session, AS_OF + timedelta(days=90))
    assert set(removed) == set(asof_table_datasets())
    assert not any(name.startswith("domain_") for name in removed)
    assert _count(db_session, "domain_metrics") == 2


def test_run_prune_calls_both_registries(db_session: Session) -> None:
    _seed_two_days(db_session)
    report = run_prune(
        db_session,
        enabled=True,
        orphan_news=False,
        orphan_reports=False,
        asof_before=__import__("datetime").datetime(2026, 12, 1),
        dry_run=True,
    )
    assert report.asof
    assert report.domain_asof["domain_metrics"] == 1
    assert report.total > 0
    assert isinstance(AS_OF, date)


def test_symbol_side_registry_still_has_fourteen_asof_datasets() -> None:
    """Counted by gate table, not by type: `search`/`lookup` are also
    `AsOfDataset` but use `discovery_asof_state`, so counting by type would
    break whenever an unrelated family grows."""
    from yfin.datasets.asof_base import GATE_TABLE, AsOfDataset

    count = sum(
        1
        for n in SYMBOL_DATASETS
        if isinstance(SYMBOL_DATASETS[n], AsOfDataset)
        and SYMBOL_DATASETS[n].asof_gate_table == GATE_TABLE
    )
    assert count == 14
