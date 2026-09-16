"""PostgreSQL write mechanics.

The dataset contract says what goes where; this says how to write it.
Datasets depend on the RowWriter protocol, not on SQLAlchemy.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import (
    Table,
    and_,
    column,
    func,
    literal_column,
    or_,
    select,
    tuple_,
    update,
    values,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from yfin.models.base import Base
from yfin.storage.changes import (
    BARS_INTERVAL_TABLES,
    BARS_TIME_COLUMN,
    ChangeCollector,
    ChangeOp,
)
from yfin.storage.contracts import TableWrite
from yfin.storage.db import returns_rows
from yfin.storage.routing import INFRASTRUCTURE_TABLES

# The IN list for multi-column verification can get very long.
VERIFY_CHUNK = 500

# Max rows per INSERT. Not a wire limit: one huge INSERT holds locks
# longer across shards, and a partial failure would roll back every row.
# Not a .env key: persistence must not import yfin.core.config.
INSERT_CHUNK = 2000

# PostgreSQL's wire protocol carries at most 65535 bind parameters per
# statement (one per column per row); exceeding it fails the whole dataset.
MAX_BIND_PARAMS = 65535


def insert_chunk_size(column_count: int) -> int:
    """Rows per INSERT that stay under the bind-parameter ceiling."""
    if column_count <= 0:
        return INSERT_CHUNK
    return max(1, min(INSERT_CHUNK, MAX_BIND_PARAMS // column_count))


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
    guard_column: str | None = None,
) -> list[dict[str, Any]]:
    """Keeps one row per key, in first-seen order; the last one wins.

    ON CONFLICT cannot touch the same row twice (21000). Monotonic columns
    take the group maximum; with a guard column the highest guard wins whole.
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
        if guard_column is not None:
            old_guard = current.get(guard_column)
            new_guard = row.get(guard_column)
            if old_guard is not None and new_guard is not None and new_guard < old_guard:
                # The incoming row is older: keep what we have, whole.
                continue
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


#: The label `RETURNING` gives the insert/update flag.
INSERTED_FLAG = "inserted"


def _returning_row(statement: Any) -> Any:
    """`RETURNING *, xmax = 0 AS inserted`.

    `*` so the consumer gets the row as the database has it. `xmax` is a
    system column, spelled as a literal; it is 0 for a freshly inserted tuple.
    """
    return statement.returning(
        literal_column("*"), literal_column("xmax = 0").label(INSERTED_FLAG)
    )


class PostgresRowWriter:
    """PostgreSQL implementation of RowWriter.

    With no collector the emitted statements must stay byte-for-byte
    unchanged; `tests/unit/test_insert_statement.py` compares them as text.
    """

    def __init__(
        self, session: Session, *, collector: ChangeCollector | None = None
    ) -> None:
        self._session = session
        self._collector = collector

    @staticmethod
    def _table(name: str) -> Table:
        return Base.metadata.tables[name]

    def _collecting(self, table_name: str) -> bool:
        """Whether this write produces events.

        Infrastructure tables are excluded here, not in the collector, so
        their statements carry no predicate and no `RETURNING *`.
        """
        return self._collector is not None and table_name not in INFRASTRUCTURE_TABLES

    def write(self, write: TableWrite) -> int:
        table = self._table(write.table)

        # The delete runs even with no rows: returning early when a
        # scope goes empty would leave the old rows behind forever.
        if write.mode == "replace_scope":
            self._delete_scope(table, write)

        if not write.rows:
            return 0

        # Both run over the whole list, before chunking: per-chunk column
        # sets would drop columns from later chunks' update scope, and a
        # key split across chunks would silently overwrite.
        rows = align_rows(write.rows)
        rows = dedupe_rows(
            rows, write.key_columns, write.monotonic_columns, write.guard_column
        )
        present = set(rows[0])
        chunk = insert_chunk_size(len(present))
        for start in range(0, len(rows), chunk):
            chunk_rows = rows[start : start + chunk]
            result = self._session.execute(
                self._insert_stmt(table, chunk_rows, write, present)
            )
            # A write whose update map is entirely volatile gets no predicate
            # and no RETURNING; reading that result raises `ResourceClosedError`.
            if self._collecting(write.table) and returns_rows(result):
                self._collect(table, write, chunk_rows, result, present)

        return self._verify(write)

    # --- statement construction --------------------------------------------

    def _effective_update_columns(
        self, table: Table, write: TableWrite, present: set[str]
    ) -> list[str]:
        """Which columns the conflict branch writes.

        A COLLECTED `replace_scope` write widens to every non-key, non-volatile
        column so the diff is equivalent to the delete-plus-insert it replaces.
        """
        if not (self._collecting(write.table) and write.mode == "replace_scope"):
            return [col for col in write.update_columns if col in present]
        return [
            col.name
            for col in table.c
            if col.name not in write.key_columns
            and col.name not in write.volatile_columns
        ]

    def _update_map(
        self, table: Table, stmt: Any, write: TableWrite, present: set[str]
    ) -> dict[str, Any]:
        """Columns the conflict branch writes, as SET expressions."""
        update_map: dict[str, Any] = {}
        for col in self._effective_update_columns(table, write, present):
            if col in write.monotonic_columns:
                # The source can report 1 for a row and 0 the next time
                # (repair heuristics depend on the window length);
                # GREATEST never writes the information back out.
                # PostgreSQL's GREATEST ignores NULL, so a one-off NULL
                # from the source also leaves the stored value alone.
                update_map[col] = func.greatest(table.c[col], stmt.excluded[col])
            else:
                update_map[col] = stmt.excluded[col]
        return update_map

    def _changed_predicate(
        self, table: Table, stmt: Any, write: TableWrite, update_map: dict[str, Any]
    ) -> Any | None:
        """`DO UPDATE ... WHERE <the row actually moved>`, or None.

        A false predicate returns nothing, so "a row came back" means "it
        changed". Volatile columns are in neither term (see `_touch_volatile`).
        """
        monotonic = [c for c in update_map if c in write.monotonic_columns]
        comparable = [
            c
            for c in update_map
            if c not in write.monotonic_columns and c not in write.volatile_columns
        ]
        terms: list[Any] = []
        if comparable:
            terms.append(
                tuple_(*(table.c[c] for c in comparable)).is_distinct_from(
                    tuple_(*(stmt.excluded[c] for c in comparable))
                )
            )
        terms.extend(
            func.greatest(table.c[c], stmt.excluded[c]).is_distinct_from(table.c[c])
            for c in monotonic
        )
        if not terms:
            return None
        return or_(*terms) if len(terms) > 1 else terms[0]

    def _insert_stmt(
        self,
        table: Table,
        rows: list[dict[str, Any]],
        write: TableWrite,
        present: set[str],
    ) -> Any:
        """INSERT ... ON CONFLICT for one chunk.

        `present` comes from all rows, not the chunk. index_elements must
        match a unique constraint exactly as a set (order does not matter).
        """
        collecting = self._collecting(write.table)
        if collecting and write.guard_column is not None:
            raise ValueError(
                f"{write.table}: guard_column cannot be combined with change "
                "collection. A guard-rejected row and an unchanged row are "
                "indistinguishable in RETURNING, so the event would claim a write "
                "that did not happen. Only the stream writer uses a guard, and it "
                "has no collector."
            )

        stmt = pg_insert(table).values(rows)
        update_map = self._update_map(table, stmt, write, present)
        predicate = (
            self._changed_predicate(table, stmt, write, update_map)
            if collecting
            else None
        )

        if update_map:
            if write.guard_column is not None:
                # Applies only when the incoming row is newer. Without
                # this a late-delivered tick would roll the stored quote
                # backwards, and the row would then disagree with the
                # tick archive it is supposed to summarise.
                return stmt.on_conflict_do_update(
                    index_elements=list(write.key_columns),
                    set_=update_map,
                    where=stmt.excluded[write.guard_column]
                    > table.c[write.guard_column],
                )
            if predicate is None:
                # No collector, or an all-volatile update map: today's
                # statement, with no returning clause to pay for.
                return stmt.on_conflict_do_update(
                    index_elements=list(write.key_columns), set_=update_map
                )
            return _returning_row(
                stmt.on_conflict_do_update(
                    index_elements=list(write.key_columns),
                    set_=update_map,
                    where=predicate,
                )
            )

        # Nothing to update: insert, and leave an existing row alone. The
        # shape is unchanged; with a collector the rows it DID insert come
        # back, because those are events.
        nothing = stmt.on_conflict_do_nothing(index_elements=list(write.key_columns))
        return _returning_row(nothing) if collecting else nothing

    # --- collecting --------------------------------------------------------

    def _collect(
        self,
        table: Table,
        write: TableWrite,
        rows: list[dict[str, Any]],
        result: Any,
        present: set[str],
    ) -> None:
        """Turns one chunk's returned rows into events, then touches the rest.

        What came back is exactly what changed: the predicate suppresses
        the rest, and `DO NOTHING` returns only what it inserted.
        """
        assert self._collector is not None  # guaranteed by `_collecting`
        returned = [dict(row) for row in result.mappings()]
        inserts: list[dict[str, Any]] = []
        updates: list[dict[str, Any]] = []
        for row in returned:
            # `pop` runs for every row, so the flag is off all of them by
            # the end -- it must not reach the consumer as a column.
            (inserts if row.pop(INSERTED_FLAG) else updates).append(row)

        if self._coalesces(write.table, inserts):
            self._record_ranges(write.table, inserts)
        else:
            for row in inserts:
                self._record_row(write, "insert", row)
        # Updates stay row-level whatever their count. On a bars table they
        # are repairs, and a repair is small and worth applying directly.
        for row in updates:
            self._record_row(write, "update", row)

        self._touch_volatile(table, write, rows, returned, present)

    def _record_row(self, write: TableWrite, op: ChangeOp, row: dict[str, Any]) -> None:
        assert self._collector is not None
        self._collector.record(
            write.table,
            op,
            {name: row[name] for name in write.key_columns},
            row,
        )

    def _coalesces(self, table_name: str, inserts: list[dict[str, Any]]) -> bool:
        """Whether this many bar inserts become spans instead of rows.

        A full history is far too many row events; steady-state daily
        writes and repairs stay row-level.
        """
        assert self._collector is not None
        return (
            table_name in BARS_TIME_COLUMN
            and len(inserts) > self._collector.range_threshold
        )

    def _record_ranges(self, table_name: str, inserts: list[dict[str, Any]]) -> None:
        """One span per (symbol, interval), or per symbol where there is no
        interval column."""
        assert self._collector is not None
        ts_column = BARS_TIME_COLUMN[table_name]
        has_interval = table_name in BARS_INTERVAL_TABLES

        spans: dict[tuple[str, str | None], list[Any]] = {}
        for row in inserts:
            key = (row["symbol"], row["bar_interval"] if has_interval else None)
            spans.setdefault(key, []).append(row[ts_column])

        for (symbol, interval), stamps in spans.items():
            self._collector.record_range(
                table_name,
                symbol,
                kind="write",
                bar_interval=interval,
                ts_column=ts_column,
                ts_from=min(stamps),
                ts_to=max(stamps),
                rows=len(stamps),
            )

    def _touch_volatile(
        self,
        table: Table,
        write: TableWrite,
        rows: list[dict[str, Any]],
        returned: list[dict[str, Any]],
        present: set[str],
    ) -> None:
        """Writes the volatile columns the predicate kept the upsert from writing.

        Otherwise `fetched_at` and `as_of_date` would freeze on unchanged rows.
        No event is emitted. Skipped when a volatile column is part of the key.
        """
        assert self._collector is not None
        volatile = [
            c
            for c in write.update_columns
            if c in present and c in write.volatile_columns and c in table.c
        ]
        if not volatile or set(volatile) & set(write.key_columns):
            return
        # `DO NOTHING` never updates anything, so there is nothing to keep
        # current: an existing row keeps the values it already had, exactly
        # as it does today.
        if not self._update_columns_present(write, present):
            return

        changed = {
            tuple(row[name] for name in write.key_columns) for row in returned
        }
        untouched = [
            row
            for row in rows
            if tuple(row[name] for name in write.key_columns) not in changed
        ]
        if not untouched:
            return

        key_cols = list(write.key_columns)
        source = [
            column(name, table.c[name].type) for name in (*key_cols, *volatile)
        ]
        for start in range(0, len(untouched), VERIFY_CHUNK):
            batch = untouched[start : start + VERIFY_CHUNK]
            data = values(*source, name="v").data(
                [tuple(row[name] for name in (*key_cols, *volatile)) for row in batch]
            )
            self._session.execute(
                update(table)
                .values({name: data.c[name] for name in volatile})
                .where(and_(*(table.c[name] == data.c[name] for name in key_cols)))
            )

    def _update_columns_present(self, write: TableWrite, present: set[str]) -> bool:
        """Whether the statement was a `DO UPDATE` rather than a `DO NOTHING`."""
        return any(col in present for col in write.update_columns)

    def _scope_predicate(self, table: Table, write: TableWrite) -> Any | None:
        """WHERE clause for the replace_scope scope, or None if it is empty.

        `scope_values` is used when the write gives it: hash-gated datasets
        can have empty rows while the scope is not.
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
            return None

        if len(cols) == 1:
            only = write.scope_columns[0]
            return cols[0].in_([scope[only] for scope in scopes])
        return tuple_(*cols).in_(
            [tuple(scope[name] for name in write.scope_columns) for scope in scopes]
        )

    def _delete_scope(self, table: Table, write: TableWrite) -> None:
        """Clears the replace_scope scope.

        Without a collector one DELETE; with one a key diff, or every row
        of every `replace_scope` table would publish as an insert each sync.
        """
        predicate = self._scope_predicate(table, write)
        if predicate is None:
            return
        if not self._collecting(write.table):
            self._session.execute(table.delete().where(predicate))
            return
        self._delete_removed_keys(table, write, predicate)

    def _delete_removed_keys(self, table: Table, write: TableWrite, predicate: Any) -> None:
        """Deletes only the keys the incoming rows no longer carry.

        `scope_columns` is a primary-key prefix on every `replace_scope`
        table, so reading the scope back is an index scan.
        """
        assert self._collector is not None
        key_columns = list(write.key_columns)
        key_cols = [table.c[name] for name in key_columns]

        stored = {
            tuple(row)
            for row in self._session.execute(select(*key_cols).where(predicate)).all()
        }
        incoming = {tuple(row[name] for name in key_columns) for row in write.rows}
        removed = sorted(stored - incoming, key=lambda key: tuple(str(v) for v in key))
        if not removed:
            return

        for start in range(0, len(removed), VERIFY_CHUNK):
            batch = removed[start : start + VERIFY_CHUNK]
            deleted = self._session.execute(
                table.delete()
                .where(tuple_(*key_cols).in_(batch))
                .returning(*key_cols)
            )
            for row in deleted.mappings():
                self._collector.record(write.table, "delete", dict(row), None)

    def _verify(self, write: TableWrite) -> int:
        """Key-existence query, as an independent read after the write.

        The affected-row count is not usable: ON CONFLICT DO NOTHING does
        not count rows it skipped.
        """
        table = self._table(write.table)
        cols = [table.c[name] for name in write.key_columns]

        if len(cols) == 1:
            values = {row[write.key_columns[0]] for row in write.rows}
            stmt = select(func.count()).select_from(table).where(cols[0].in_(values))
            return int(self._session.execute(stmt).scalar_one())

        # Row-constructor IN: uses the primary key index. The key list MUST
        # be deduplicated: the chunks are separate IN lists, so a key in two
        # chunks would be counted twice and inflate `verified`.
        keys = list(
            dict.fromkeys(tuple(row[name] for name in write.key_columns) for row in write.rows)
        )
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
