"""Hash-gated dataset base.

`SnapshotDataset` CANNOT be used for this: there, the table compared
(`snapshot_table`) and the table written (`history_table`) are DIFFERENT,
and what gets skipped is rows of the history table. Here the compared table
and the written table are the SAME (`financial_periods`), and what gets
skipped is the CHILD table (`financial_facts`).

Rule:
1. The hash query runs before anything else.
2. If the hash matches, child rows are never written -> skipped.
3. The header row is ALWAYS written; if the hash matches, only fetched_at
   is updated. So fetched_at means "last verified at".
"""

from __future__ import annotations

from typing import Any

from yfin.datasets.base import Dataset, NormalizedResult, TableWrite, WriteStats
from yfin.storage.persistence import RowWriter, apply_write

# When the hash is unchanged, only this column is updated on the header row.
UNCHANGED_UPDATE_COLUMNS = ("fetched_at",)


class HashGate:
    """Hash gate -- a mixin INDEPENDENT of the `Dataset` hierarchy.

    Same reasoning as splitting out `AsOfGate`: the gate logic is independent
    of the fetch/normalize signature, but not of the class hierarchy.
    `HashGatedDataset` sits under `Dataset[RawT]` with the
    `fetch(SyncContext)` / `normalize(raw, symbol)` signature; the screener
    side is `GlobalDataset` with `fetch(MarketContext)` / `normalize(raw)`.
    The two hierarchies CANNOT be merged.

    The mixin touches neither signature; it only provides `upsert`.
    Behavior is IDENTICAL to before the split -- the existing
    `financial_statements` tests carry this.
    """

    gate_table: str
    child_table: str
    gate_key_columns: tuple[str, ...]

    def _key(self, row: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(row[name] for name in self.gate_key_columns)

    def upsert(self, writer: RowWriter, result: NormalizedResult) -> WriteStats:
        stats = WriteStats(skipped=dict(result.skipped))
        gate_writes = [w for w in result.writes if w.table == self.gate_table]
        child_writes = [w for w in result.writes if w.table == self.child_table]
        other_writes = [
            w for w in result.writes if w.table not in (self.gate_table, self.child_table)
        ]

        # 1. Hash queries, before ANY write.
        unchanged: set[tuple[Any, ...]] = set()
        changed: list[dict[str, Any]] = []
        for write in gate_writes:
            for row in write.rows:
                key = {name: row[name] for name in self.gate_key_columns}
                current = writer.current_hash(self.gate_table, key)
                if current == row["content_hash"]:
                    unchanged.add(self._key(row))
                else:
                    changed.append(row)

        # 2. Header rows: full write for changed, fetched_at only for unchanged.
        for write in gate_writes:
            changed_rows = [row for row in write.rows if self._key(row) not in unchanged]
            unchanged_rows = [row for row in write.rows if self._key(row) in unchanged]
            if changed_rows:
                apply_write(writer, _with_rows(write, changed_rows), stats)
            if unchanged_rows:
                apply_write(
                    writer,
                    _with_rows(write, unchanged_rows, update_columns=UNCHANGED_UPDATE_COLUMNS),
                    stats,
                )

        # 3. Child rows: only for changed periods.
        # Delete scope is derived from the changed header keys, NOT from the
        # rows -- otherwise a period whose items all came back NaN (rows
        # empty) would leave its old rows in place permanently.
        scope_values = tuple(
            {name: row[name] for name in self.gate_key_columns} for row in changed
        )
        for write in child_writes:
            kept = [row for row in write.rows if self._key(row) not in unchanged]
            dropped = len(write.rows) - len(kept)
            if dropped:
                stats.skipped[write.table] = stats.skipped.get(write.table, 0) + dropped
            if not scope_values:
                # No period changed: neither delete nor write happens.
                stats.attempted.setdefault(write.table, 0)
                stats.verified.setdefault(write.table, 0)
                continue
            apply_write(
                writer,
                TableWrite(
                    table=write.table,
                    rows=kept,
                    key_columns=write.key_columns,
                    update_columns=write.update_columns,
                    mode="replace_scope",
                    scope_columns=self.gate_key_columns,
                    scope_values=scope_values,
                ),
                stats,
            )

        for write in other_writes:
            apply_write(writer, write, stats)
        return stats


class HashGatedDataset[RawT](HashGate, Dataset[RawT]):
    """Hash-gated dataset on the symbol axis.

    The body moved to `HashGate`; this class only combines the two sides
    and leaves existing call sites unchanged.
    """


def _with_rows(
    write: TableWrite,
    rows: list[dict[str, Any]],
    *,
    update_columns: tuple[str, ...] | None = None,
) -> TableWrite:
    return TableWrite(
        table=write.table,
        rows=rows,
        key_columns=write.key_columns,
        update_columns=update_columns if update_columns is not None else write.update_columns,
        mode=write.mode,
        scope_columns=write.scope_columns,
        scope_values=write.scope_values,
    )
