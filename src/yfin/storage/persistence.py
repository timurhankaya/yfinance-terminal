"""PostgreSQL write mechanics.

Separate from the dataset contract (datasets/base.py): the contract says
what goes where, this says how to write it. Datasets depend on the
RowWriter protocol, not on SQLAlchemy.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from sqlalchemy import Table, and_, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from yfin.datasets.base import TableWrite, WriteStats
from yfin.models.base import Base

# The IN list for multi-column verification can get very long.
VERIFY_CHUNK = 500

# Max rows per INSERT. bars_1m produces ~20,000 rows per symbol on the
# first backfill. Not about packet size -- PostgreSQL has no such limit.
# Two real reasons: one huge INSERT holds locks longer across shards, and
# a partial failure would roll back all 20,000 rows instead of leaving
# the completed chunks behind.
#
# Deliberately not a .env key: persistence imports nothing from
# yfin.core.config and should not gain a configuration dependency.
INSERT_CHUNK = 2000


class RowSink(Protocol):
    """Write-only capability.

    apply_write and plain upsert datasets need nothing more; code that
    reads no hashes and looks up no symbols depends on this narrow view.
    """

    def write(self, write: TableWrite) -> int:
        """Writes the rows and returns the count of *verified* rows."""
        ...


class HashReader(Protocol):
    """Read capability for the snapshot/hash gate."""

    def current_hash(self, table: str, key: Mapping[str, Any]) -> str | None:
        """Current content_hash at that key, or None.

        The key can span several columns: market_summary is
        (region, board_code), financial_periods is
        (symbol, statement, freq, period_end).
        """
        ...


class SymbolLookup(Protocol):
    """Lookup capability, used to flag out-of-universe symbols."""

    def known_symbols(self, candidates: set[str]) -> set[str]:
        """Those of the candidates that exist in `symbols`."""
        ...


class SnapshotWriter(RowSink, HashReader, Protocol):
    """What snapshot and hash-gated datasets see."""


class RowWriter(RowSink, HashReader, SymbolLookup, Protocol):
    """The full interface the dataset contract sees.

    Concrete PostgreSQL details (ON CONFLICT, the key-existence query)
    stay behind this protocol. Dataset.upsert takes the full interface;
    internal helpers depend on the narrowest one they need.
    """


def apply_write(writer: RowSink, write: TableWrite, stats: WriteStats) -> None:
    """Applies one TableWrite and updates the stats."""
    stats.attempted[write.table] = stats.attempted.get(write.table, 0) + len(write.rows)
    stats.verified[write.table] = stats.verified.get(write.table, 0) + writer.write(write)


def align_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aligns every row to the same column set.

    SQLAlchemy takes the column names from the first row of a multi-row
    INSERT; columns that only appear later are silently dropped.
    """
    columns: dict[str, None] = {}
    for row in rows:
        for key in row:
            columns[key] = None
    if all(len(row) == len(columns) for row in rows):
        return rows
    return [{col: row.get(col) for col in columns} for row in rows]


def dedupe_rows(
    rows: list[dict[str, Any]],
    key_columns: tuple[str, ...],
    monotonic_columns: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Keeps one row per key; the last one wins.

    Required: ON CONFLICT DO UPDATE cannot touch the same row twice in
    one statement (21000, "cannot affect row a second time"). Most
    datasets make no in-batch uniqueness guarantee -- only four of the 57
    TableWrite call sites deduplicate on their own.

    monotonic_columns are the exception and take the group maximum.
    GREATEST only compares the incoming row against the row already in
    the database, never two rows of the same batch, so plain last-wins
    would let a monotonic column regress within a chunk.

    First-seen order is preserved.
    """
    if len(rows) < 2:
        return rows
    seen: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key = tuple(row.get(name) for name in key_columns)
        current = seen.get(key)
        if current is None:
            seen[key] = dict(row)
            continue
        merged = {**current, **row}
        for col in monotonic_columns:
            old, new = current.get(col), row.get(col)
            if old is None:
                merged[col] = new
            elif new is None:
                merged[col] = old
            else:
                merged[col] = max(old, new)
        seen[key] = merged
    if len(seen) == len(rows):
        return rows
    return list(seen.values())


class PostgresRowWriter:
    """PostgreSQL implementation of RowWriter."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @staticmethod
    def _table(name: str) -> Table:
        return Base.metadata.tables[name]

    def write(self, write: TableWrite) -> int:
        table = self._table(write.table)

        # The delete runs even with no rows: returning early when a
        # scope goes empty would leave the old rows behind forever.
        if write.mode == "replace_scope":
            self._delete_scope(table, write)

        if not write.rows:
            return 0

        # Both of these run over the whole list, before chunking.
        # Aligning afterwards would give each chunk its own column set and
        # its own update map, so a column present in the first chunk would
        # silently drop out of the second one's update scope. Deduping
        # afterwards would not raise 21000, but two rows with the same key
        # landing in different chunks means the second overwrites the
        # first -- silent data loss.
        rows = align_rows(write.rows)
        rows = dedupe_rows(rows, write.key_columns, write.monotonic_columns)
        present = set(rows[0])
        for start in range(0, len(rows), INSERT_CHUNK):
            self._session.execute(
                self._insert_stmt(table, rows[start : start + INSERT_CHUNK], write, present)
            )

        return self._verify(write)

    def _insert_stmt(
        self,
        table: Table,
        rows: list[dict[str, Any]],
        write: TableWrite,
        present: set[str],
    ) -> Any:
        """INSERT ... ON CONFLICT for one chunk.

        `present` is derived from all rows and passed in; computing it
        per chunk would undo what align_rows just did.

        index_elements must match a unique constraint exactly as a set --
        a subset and a superset both raise "there is no unique or
        exclusion constraint matching the ON CONFLICT specification"
        (order does not matter). test_persistence_contract keeps
        key_columns honest.
        """
        stmt = pg_insert(table).values(rows)
        # The update scope is narrowed to columns actually present: the
        # column set varies per symbol (a non-fund has no 'Capital
        # Gains').
        update_map: dict[str, Any] = {}
        for col in write.update_columns:
            if col not in present:
                continue
            if col in write.monotonic_columns:
                # The source can report 1 for a row and 0 the next time
                # (repair heuristics depend on the window length);
                # GREATEST never writes the information back out.
                # PostgreSQL's GREATEST ignores NULL, so a one-off NULL
                # from the source also leaves the stored value alone.
                update_map[col] = func.greatest(table.c[col], stmt.excluded[col])
            else:
                update_map[col] = stmt.excluded[col]
        if update_map:
            return stmt.on_conflict_do_update(
                index_elements=list(write.key_columns), set_=update_map
            )
        # Nothing to update: insert, and leave an existing row alone.
        return stmt.on_conflict_do_nothing(index_elements=list(write.key_columns))

    def _delete_scope(self, table: Table, write: TableWrite) -> None:
        """Deletes the replace_scope scope.

        scope_columns defines it, defaulting to ("symbol",):
        company_officers works per symbol, financial_facts per
        (symbol, statement, freq, period_end).
        """
        cols = [table.c[name] for name in write.scope_columns]
        if write.scope_values is not None:
            scopes: list[Mapping[str, Any]] = list(write.scope_values)
        else:
            seen: dict[tuple[Any, ...], Mapping[str, Any]] = {}
            for row in write.rows:
                key = tuple(row[name] for name in write.scope_columns)
                seen[key] = {name: row[name] for name in write.scope_columns}
            scopes = list(seen.values())
        if not scopes:
            return

        if len(cols) == 1:
            values = [scope[write.scope_columns[0]] for scope in scopes]
            self._session.execute(table.delete().where(cols[0].in_(values)))
            return
        values_multi = [tuple(scope[name] for name in write.scope_columns) for scope in scopes]
        self._session.execute(table.delete().where(tuple_(*cols).in_(values_multi)))

    def _verify(self, write: TableWrite) -> int:
        """Key-existence query.

        The affected-row count is not usable for verification: ON
        CONFLICT DO NOTHING does not count rows it skipped (measured:
        INSERT 0 0). This asks the stronger question -- how many of the
        requested keys are actually in the table -- as an independent
        read after the write.
        """
        table = self._table(write.table)
        cols = [table.c[name] for name in write.key_columns]

        if len(cols) == 1:
            values = {row[write.key_columns[0]] for row in write.rows}
            stmt = select(func.count()).select_from(table).where(cols[0].in_(values))
            return int(self._session.execute(stmt).scalar_one())

        # Row-constructor IN: markedly faster than OR/AND blocks and
        # still uses the primary key index.
        keys = [tuple(row[name] for name in write.key_columns) for row in write.rows]
        total = 0
        for start in range(0, len(keys), VERIFY_CHUNK):
            stmt = (
                select(func.count())
                .select_from(table)
                .where(tuple_(*cols).in_(keys[start : start + VERIFY_CHUNK]))
            )
            total += int(self._session.execute(stmt).scalar_one())
        return total

    def current_hash(self, table: str, key: Mapping[str, Any]) -> str | None:
        target = self._table(table)
        stmt = select(target.c["content_hash"]).where(
            and_(*(target.c[name] == value for name, value in key.items()))
        )
        value = self._session.execute(stmt).scalar_one_or_none()
        return str(value) if value is not None else None

    def known_symbols(self, candidates: set[str]) -> set[str]:
        from yfin.models.symbols import Symbol

        if not candidates:
            return set()
        stmt = select(Symbol.symbol).where(Symbol.symbol.in_(candidates))
        return set(self._session.execute(stmt).scalars())
