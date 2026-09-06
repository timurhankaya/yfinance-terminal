"""Common base for snapshot datasets.

The snapshot table updates its row; the _history table adds a new row ONLY
if content_hash changed. When unchanged, that's a 'skipped', not an error.

The compared table (snapshot) and the written table (_history) are
DIFFERENT, so "compare first, then write" is safe. For a hash gate that
writes to the same table, use `HashGatedDataset` instead.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from yfin.datasets.base import Dataset, NormalizedResult
from yfin.storage.contracts import RowWriter, SnapshotWriter, TableWrite, WriteStats, apply_write


def snapshot_upsert(
    writer: SnapshotWriter,
    result: NormalizedResult,
    *,
    snapshot_table: str,
    history_table: str,
    key_columns: tuple[str, ...],
) -> WriteStats:
    """Snapshot + history write; works keyed on symbol or region."""
    stats = WriteStats(skipped=dict(result.skipped))
    history_writes: list[TableWrite] = []
    other_writes: list[TableWrite] = []

    for write in result.writes:
        (history_writes if write.table == history_table else other_writes).append(write)

    # The hash comparison happens BEFORE the snapshot table is updated;
    # otherwise the comparison would always match and _history would never
    # get a new row.
    keep: list[TableWrite] = []
    for write in history_writes:
        kept: list[dict[str, Any]] = []
        skipped = 0
        for row in write.rows:
            key = {name: row[name] for name in key_columns}
            current = writer.current_hash(snapshot_table, key)
            if current == row["content_hash"]:
                skipped += 1
            else:
                kept.append(row)
        if skipped:
            stats.skipped[write.table] = stats.skipped.get(write.table, 0) + skipped
        keep.append(replace(write, rows=kept))

    for write in other_writes:
        apply_write(writer, write, stats)
    for write in keep:
        apply_write(writer, write, stats)

    return stats


class SnapshotDataset[RawT](Dataset[RawT]):
    """Writes to two tables: current snapshot + history.

    The hash comparison needs a DB read, so it happens in upsert(), not
    normalize(); normalize stays pure.
    """

    snapshot_table: str
    history_table: str
    # Snapshot table's key columns: ("symbol",) for ticker_*, ("region",)
    # for market_status, ("region", "board_code") for market_summary.
    key_columns: tuple[str, ...] = ("symbol",)

    def upsert(self, writer: RowWriter, result: NormalizedResult) -> WriteStats:
        return snapshot_upsert(
            writer,
            result,
            snapshot_table=self.snapshot_table,
            history_table=self.history_table,
            key_columns=self.key_columns,
        )
