"""Common base for snapshot datasets.

The snapshot table updates its row; _history adds a row only if content_hash
changed (else 'skipped'). A gate on the written table itself is `HashGatedDataset`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any, Protocol

from yfin.datasets.base import Dataset, NormalizedResult
from yfin.storage.contracts import RowWriter, SnapshotWriter, TableWrite, WriteStats, apply_write


class SnapshotSpec(Protocol):
    """What a snapshot dataset declares about where its rows go.

    A Protocol because the symbol and market snapshot hierarchies share
    no ancestor.
    """

    # Read-only properties, not plain attributes: a mutable Protocol member
    # is INVARIANT, so `key_columns = ("region", "board_code")` on
    # `MarketSummaryDataset` would not match `tuple[str, ...]`. Nothing here
    # writes to them, so declaring that is both true and what makes the
    # concrete tuples fit.
    @property
    def snapshot_table(self) -> str: ...

    @property
    def history_table(self) -> str: ...

    @property
    def key_columns(self) -> tuple[str, ...]: ...


def snapshot_writes(
    dataset: SnapshotSpec,
    rows: Sequence[dict[str, Any]],
    *,
    snapshot_update: tuple[str, ...],
    history_update: tuple[str, ...] | None = None,
) -> list[TableWrite]:
    """The snapshot write and the history write, from what the class DECLARES.

    The history key is the snapshot key plus `fetched_at`. `history_update`
    defaults to `snapshot_update`; narrow it when `fetched_at` is in the history PK.
    """
    return [
        TableWrite(
            table=dataset.snapshot_table,
            rows=[dict(row) for row in rows],
            key_columns=dataset.key_columns,
            update_columns=snapshot_update,
        ),
        TableWrite(
            table=dataset.history_table,
            # Copied again, not shared: `apply_write` may rewrite a row in
            # place (mark_known does), and the two writes must not become
            # each other's aliases.
            rows=[dict(row) for row in rows],
            key_columns=(*dataset.key_columns, "fetched_at"),
            update_columns=history_update if history_update is not None else snapshot_update,
        ),
    ]


def snapshot_upsert(
    writer: SnapshotWriter,
    result: NormalizedResult,
    *,
    snapshot_table: str,
    history_table: str,
    key_columns: tuple[str, ...],
    full_refresh: bool = False,
) -> WriteStats:
    """Snapshot + history write; works keyed on symbol or region.

    `full_refresh` skips the snapshot comparison so missing history rows
    can be repaired while the snapshot row still exists.
    """
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
            if full_refresh:
                kept.append(row)
                continue
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


class SnapshotWrite:
    """The snapshot + history write policy, declared ONCE for both axes.

    `key_columns` has no default on purpose: a market dataset must not end
    up keyed by a `symbol` column it does not have.
    """

    #: The table holding one row per key: what the gate compares against.
    snapshot_table: str
    #: The table holding one row per key per fetch.
    history_table: str
    #: The snapshot's key: ("symbol",) for ticker_*, ("region",) for
    #: market_status, ("region", "board_code") for market_summary.
    key_columns: tuple[str, ...]

    def upsert(
        self, writer: RowWriter, result: NormalizedResult, *, full_refresh: bool = False
    ) -> WriteStats:
        return snapshot_upsert(
            writer,
            result,
            snapshot_table=self.snapshot_table,
            history_table=self.history_table,
            key_columns=self.key_columns,
            full_refresh=full_refresh,
        )


class SnapshotDataset[RawT](SnapshotWrite, Dataset[RawT]):
    """A symbol-scoped dataset that writes a snapshot + history.

    The hash comparison needs a DB read, so it happens in upsert(), not
    normalize(); normalize stays pure.
    """
