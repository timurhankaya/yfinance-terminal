"""Exit code and status logic tests."""

from __future__ import annotations

import pytest

from yfin.models import ItemStatus
from yfin.pipeline.runner import (
    EXIT_ALL_FAILED,
    EXIT_NO_SYMBOL_RESOLVED,
    EXIT_OK,
    EXIT_PARTIAL,
    ItemRecord,
    RunSummary,
)


def _summary(statuses: list[tuple[str, ItemStatus]], symbol_count: int = 1) -> RunSummary:
    items = [ItemRecord(symbol=symbol, dataset="d", status=status) for symbol, status in statuses]
    return RunSummary(run_id=1, items=items, symbol_count=symbol_count, dataset_count=1)


def test_empty_and_skipped_do_not_fail_the_run() -> None:
    """No failed -> 0; ok U empty U skipped is normal."""
    summary = _summary([("A", ItemStatus.OK), ("A", ItemStatus.EMPTY), ("A", ItemStatus.SKIPPED)])
    assert summary.exit_code() == EXIT_OK


def test_all_empty_is_still_zero() -> None:
    """capital_gains never comes back populated for any symbol; this is not an error."""
    assert _summary([("A", ItemStatus.EMPTY)]).exit_code() == EXIT_OK


def test_partial_failure() -> None:
    summary = _summary([("A", ItemStatus.OK), ("A", ItemStatus.FAILED)])
    assert summary.exit_code() == EXIT_PARTIAL


def test_all_cells_failed() -> None:
    summary = _summary([("A", ItemStatus.FAILED), ("A", ItemStatus.FAILED)])
    assert summary.exit_code() == EXIT_ALL_FAILED


def test_no_symbol_resolved() -> None:
    summary = _summary([("A", ItemStatus.UNKNOWN_SYMBOL)])
    assert summary.exit_code() == EXIT_NO_SYMBOL_RESOLVED


def test_one_resolved_one_not_is_not_exit_one() -> None:
    summary = _summary([("A", ItemStatus.OK), ("B", ItemStatus.UNKNOWN_SYMBOL)], symbol_count=2)
    assert summary.exit_code() == EXIT_OK


def test_totals_sum_all_items() -> None:
    items = [
        ItemRecord("A", "d", ItemStatus.OK, rows_fetched=5, rows_written=5, rows_verified=5),
        ItemRecord("A", "e", ItemStatus.SKIPPED, rows_skipped=1),
    ]
    summary = RunSummary(run_id=1, items=items, symbol_count=1, dataset_count=2)
    assert summary.totals() == {
        "rows_fetched": 5,
        "rows_written": 5,
        "rows_verified": 5,
        "rows_skipped": 1,
    }


class TestItemStatusMapping:
    """_record_items status table."""

    @pytest.mark.parametrize(
        ("attempted", "verified", "skipped", "expected"),
        [
            (0, 0, 0, ItemStatus.EMPTY),
            (0, 0, 3, ItemStatus.SKIPPED),
            (5, 5, 0, ItemStatus.OK),
            (5, 4, 0, ItemStatus.FAILED),
        ],
    )
    def test_status_from_counts(
        self, attempted: int, verified: int, skipped: int, expected: ItemStatus
    ) -> None:
        from yfin.datasets.base import Dataset, WriteStats
        from yfin.pipeline.runner import _record_items

        class Dummy(Dataset):
            name = "dummy"
            produces = ("t",)

            def fetch(self, ctx: object) -> object:
                return None

            def normalize(self, raw: object, symbol: str) -> object:
                return None

        stats = WriteStats(
            attempted={"t": attempted} if attempted else {},
            verified={"t": verified} if verified else {},
            skipped={"t": skipped} if skipped else {},
        )
        records = _record_items(Dummy(), "A", stats, attempted, 0)
        assert records[0].status is expected

    def test_multi_table_dataset_writes_one_row_per_table(self) -> None:
        """For datasets writing to multiple tables, one row per table."""
        from yfin.datasets import SYMBOL_DATASETS as REGISTRY
        from yfin.datasets.base import WriteStats
        from yfin.pipeline.runner import _record_items

        stats = WriteStats(
            attempted={"ticker_info": 1, "ticker_info_history": 1, "company_officers": 10},
            verified={"ticker_info": 1, "ticker_info_history": 1, "company_officers": 10},
        )
        records = _record_items(REGISTRY["info"], "AAPL", stats, 1, 5)
        assert {r.table_name for r in records} == {
            "ticker_info",
            "ticker_info_history",
            "company_officers",
        }
        assert all(r.status is ItemStatus.OK for r in records)
