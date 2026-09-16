"""Hash-gated dataset base.

Compared and written table are the same; the hash skips the child table. The
header row is always written; unchanged, only fetched_at ("last verified").
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from yfin.datasets.base import Dataset, NormalizedResult
from yfin.storage.contracts import RowWriter, TableWrite, WriteStats, apply_write

# When the hash is unchanged, only this column is updated on the header row.
UNCHANGED_UPDATE_COLUMNS = ("fetched_at",)


class HashGate:
    """Hash gate -- a mixin INDEPENDENT of the `Dataset` hierarchy.

    Shared by `HashGatedDataset` and the screener's `GlobalDataset`, whose
    fetch/normalize signatures differ; the mixin only provides `upsert`.
    """

    gate_table: str
    child_table: str
    gate_key_columns: tuple[str, ...]

    def _key(self, row: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(row[name] for name in self.gate_key_columns)

    def upsert(
        self, writer: RowWriter, result: NormalizedResult, *, full_refresh: bool = False
    ) -> WriteStats:
        stats = WriteStats(skipped=dict(result.skipped))
        gate_writes = [w for w in result.writes if w.table == self.gate_table]
        child_writes = [w for w in result.writes if w.table == self.child_table]
        other_writes = [
            w for w in result.writes if w.table not in (self.gate_table, self.child_table)
        ]

        # 1. Hash queries, before ANY write. `--full-refresh` skips them:
        #    every period counts as changed, so the child rows are written
        #    even when the header hash still matches. Without this the flag
        #    cannot repair a period whose `financial_facts` were lost while
        #    its `financial_periods` header survived.
        unchanged: set[tuple[Any, ...]] = set()
        changed: list[dict[str, Any]] = []
        for write in gate_writes:
            for row in write.rows:
                if full_refresh:
                    changed.append(row)
                    continue
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
                replace(
                    write,
                    rows=kept,
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

    Combines `HashGate` with the symbol-axis `Dataset`.
    """


def _with_rows(
    write: TableWrite,
    rows: list[dict[str, Any]],
    *,
    update_columns: tuple[str, ...] | None = None,
) -> TableWrite:
    if update_columns is None:
        return replace(write, rows=rows)
    return replace(write, rows=rows, update_columns=update_columns)
