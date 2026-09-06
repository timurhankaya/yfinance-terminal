"""`ItemRecord.region` genislemesi SIFIR DAVRANIS DEGISIKLIGI (SI S9.2, S14/2)."""

from __future__ import annotations

from yfin.datasets import MARKET_DATASETS, SYMBOL_DATASETS
from yfin.datasets.base import WriteStats
from yfin.models import ItemStatus
from yfin.runner import ItemRecord, _failed_records, _record_items, _skipped_records


def _stats() -> WriteStats:
    return WriteStats(attempted={"institutional_holders": 3}, verified={"institutional_holders": 3})


def test_region_defaults_to_none() -> None:
    assert ItemRecord(symbol="AAPL", dataset="info", status=ItemStatus.OK).region is None


def test_symbol_side_records_leave_region_null() -> None:
    dataset = SYMBOL_DATASETS["institutional_holders"]
    records = _record_items(dataset, "AAPL", _stats(), fetched=3, duration_ms=10)
    assert records
    assert all(r.region is None for r in records)


def test_market_side_records_leave_region_null() -> None:
    """`market_runner` bolgeyi `symbol` alanina yaziyor ve bu BILINCLI olarak
    degistirilmedi (SI S5.9): degistirmek mevcut denetim sorgularini kirardi."""
    dataset = MARKET_DATASETS["market_summary"]
    stats = WriteStats(attempted={"market_summary": 5}, verified={"market_summary": 5})
    records = _record_items(dataset, "US", stats, fetched=5, duration_ms=7)
    assert all(r.region is None for r in records)
    assert {r.symbol for r in records} == {"US"}


def test_failed_and_skipped_records_accept_region() -> None:
    from yfin.datasets.registry import DOMAIN_DATASETS

    failed = _failed_records(
        "^YH311", "sector_rankings", "boom", DOMAIN_DATASETS, region="GB"
    )
    assert {r.region for r in failed} == {"GB"}
    assert {r.table_name for r in failed} == set(DOMAIN_DATASETS["sector_rankings"].produces)

    skipped = _skipped_records(
        "^YH311", "sector_rankings", "neden", DOMAIN_DATASETS, region="GB"
    )
    assert {r.region for r in skipped} == {"GB"}
    assert {r.status for r in skipped} == {ItemStatus.SKIPPED}


def test_failed_records_without_region_are_unchanged() -> None:
    records = _failed_records("AAPL", "info", "boom")
    assert all(r.region is None for r in records)
    assert {r.status for r in records} == {ItemStatus.FAILED}


def test_domain_records_carry_the_region() -> None:
    from yfin.datasets.registry import DOMAIN_DATASETS

    dataset = DOMAIN_DATASETS["sector_rankings"]
    stats = WriteStats(
        attempted={"domain_top_companies": 50, "domain_asof_state": 1},
        verified={"domain_top_companies": 50, "domain_asof_state": 1},
    )
    records = _record_items(dataset, "^YH311", stats, fetched=51, duration_ms=12, region="GB")
    assert {r.region for r in records} == {"GB"}
    assert {r.symbol for r in records} == {"^YH311"}
    assert all(r.status is ItemStatus.OK for r in records)
