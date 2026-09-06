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
    RunTally,
)


def _tally(statuses: list[tuple[str, ItemStatus]], symbol_count: int = 1) -> RunTally:
    """Builds the tally the way `finalize_run` does: counts per status,
    plus how many distinct symbols actually resolved."""
    counts: dict[str, int] = {}
    for _symbol, status in statuses:
        counts[status.value] = counts.get(status.value, 0) + 1
    unresolved = {s for s, st in statuses if st is ItemStatus.UNKNOWN_SYMBOL}
    resolved = len({s for s, _ in statuses} - unresolved)
    return RunTally(
        run_id=1,
        symbol_count=symbol_count,
        dataset_count=1,
        resolved_symbols=resolved,
        counts=counts,
    )


def test_empty_and_skipped_do_not_fail_the_run() -> None:
    """No failed -> 0; ok U empty U skipped is normal."""
    tally = _tally([("A", ItemStatus.OK), ("A", ItemStatus.EMPTY), ("A", ItemStatus.SKIPPED)])
    assert tally.exit_code() == EXIT_OK


def test_all_empty_is_still_zero() -> None:
    """capital_gains never comes back populated for any symbol; this is not an error."""
    assert _tally([("A", ItemStatus.EMPTY)]).exit_code() == EXIT_OK


def test_partial_failure() -> None:
    tally = _tally([("A", ItemStatus.OK), ("A", ItemStatus.FAILED)])
    assert tally.exit_code() == EXIT_PARTIAL


def test_all_cells_failed() -> None:
    tally = _tally([("A", ItemStatus.FAILED), ("A", ItemStatus.FAILED)])
    assert tally.exit_code() == EXIT_ALL_FAILED


def test_no_symbol_resolved() -> None:
    tally = _tally([("A", ItemStatus.UNKNOWN_SYMBOL)])
    assert tally.exit_code() == EXIT_NO_SYMBOL_RESOLVED


def test_one_resolved_one_not_is_not_exit_one() -> None:
    tally = _tally([("A", ItemStatus.OK), ("B", ItemStatus.UNKNOWN_SYMBOL)], symbol_count=2)
    assert tally.exit_code() == EXIT_OK


def test_a_not_attempted_cell_can_never_report_success() -> None:
    """A run that left work undone must not exit 0, even with no failures.

    This is the rule the retired RunSummary.exit_code() did not have: it
    returned EXIT_OK here, so a shard that died before touching its queue
    looked like a clean run.
    """
    tally = _tally([("A", ItemStatus.OK), ("B", ItemStatus.NOT_ATTEMPTED)], symbol_count=2)
    assert tally.exit_code() == EXIT_PARTIAL


def test_item_record_carries_the_row_counters() -> None:
    record = ItemRecord("A", "d", ItemStatus.OK, rows_fetched=5, rows_written=5, rows_verified=5)
    assert (record.rows_fetched, record.rows_written, record.rows_verified) == (5, 5, 5)


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
