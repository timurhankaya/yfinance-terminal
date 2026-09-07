"""PostgreSQL write mechanics.

Separate from the dataset contract (datasets/base.py): the contract says
what goes where, this says how to write it. Datasets depend on the
RowWriter protocol, not on SQLAlchemy.
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

# Max rows per INSERT. bars_1m produces ~20,000 rows per symbol on the
# first backfill. Not about packet size -- PostgreSQL has no such limit.
# Two real reasons: one huge INSERT holds locks longer across shards, and
# a partial failure would roll back all 20,000 rows instead of leaving
# the completed chunks behind.
#
# Deliberately not a .env key: persistence imports nothing from
# yfin.core.config and should not gain a configuration dependency.
INSERT_CHUNK = 2000

# PostgreSQL's wire protocol carries at most 65535 bind parameters per
# statement, and a multi-row INSERT binds one per column per row. This is
# a HARD limit, unlike INSERT_CHUNK: exceeding it raises
# psycopg.OperationalError "number of parameters must be between 0 and
# 65535" and the whole dataset fails. Measured: screen_quotes has 107
# columns, so 2000 rows asked for 214,000 parameters and every screener
# run died. pymysql interpolated client-side and never hit this, which is
# why the limit only appeared after the PostgreSQL migration.
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
    """Keeps one row per key; the last one wins.

    Required: ON CONFLICT DO UPDATE cannot touch the same row twice in
    one statement (21000, "cannot affect row a second time"). Most
    datasets make no in-batch uniqueness guarantee -- only four of the 57
    TableWrite call sites deduplicate on their own.

    monotonic_columns are the exception and take the group maximum.
    GREATEST only compares the incoming row against the row already in
    the database, never two rows of the same batch, so plain last-wins
    would let a monotonic column regress within a chunk.

    guard_column is a second exception, and it decides between whole rows
    rather than merging them: the row with the highest guard value wins
    outright. Last-wins would not do, because the database guard only
    sees the row this function hands it -- an out-of-order tick arriving
    later in the same batch would be the one compared, and the newer
    value would already be gone.

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

    `*` rather than a column list, so what the consumer receives is the row
    as the DATABASE has it: columns outside the update map, `GREATEST`-merged
    columns and server defaults included, rather than what the pipeline
    proposed.

    `xmax` is a system column and is not in `Base.metadata`, so it can only
    be spelled as a literal -- `table.c.xmax` raises `AttributeError`. A
    tuple written by the INSERT branch has `xmax = 0`; one rewritten by
    `DO UPDATE` carries the updating transaction's id. Measured on a plain
    table, a hypertable chunk and a same-transaction re-upsert; see
    docs/measurements/database.md.
    """
    return statement.returning(
        literal_column("*"), literal_column("xmax = 0").label(INSERTED_FLAG)
    )


class PostgresRowWriter:
    """PostgreSQL implementation of RowWriter.

    With no collector -- which is every call site until the runners are
    wired up, and every call site forever when `yf_changes_enabled` is off
    -- the statements it emits are byte-for-byte what they have always been.
    `tests/unit/test_insert_statement.py` compares them as text, because
    this one statement writes 68 tables and a silent change to it is the
    most expensive kind this codebase can make.
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

        Infrastructure tables are excluded HERE rather than inside the
        collector, because the difference has to reach the statement: the
        gate rows carry `GATE_UPDATE_COLUMNS`, and a predicate plus a
        `RETURNING *` for a row nobody receives is pure cost.
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

        # Both of these run over the whole list, before chunking.
        # Aligning afterwards would give each chunk its own column set and
        # its own update map, so a column present in the first chunk would
        # silently drop out of the second one's update scope. Deduping
        # afterwards would not raise 21000, but two rows with the same key
        # landing in different chunks means the second overwrites the
        # first -- silent data loss.
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
            # `_collecting` is not enough. A write whose update map is
            # ENTIRELY volatile gets no predicate and therefore no
            # `RETURNING` -- the hash gate's `UNCHANGED_UPDATE_COLUMNS =
            # ("fetched_at",)` write is exactly that, on a data table, so it
            # is collected in principle and returns nothing in practice.
            # Reading it raises `ResourceClosedError` and takes the whole
            # symbol transaction with it. `returns_rows` is the statement
            # asked whether it has a RETURNING clause, which is the question.
            if self._collecting(write.table) and returns_rows(result):
                self._collect(table, write, chunk_rows, result, present)

        return self._verify(write)

    # --- statement construction --------------------------------------------

    def _effective_update_columns(
        self, table: Table, write: TableWrite, present: set[str]
    ) -> list[str]:
        """Which columns the conflict branch writes, and whether `present`
        narrows them.

        Normally the declared `update_columns`, narrowed to what the rows
        actually carry: the column set varies per symbol (a non-fund has no
        'Capital Gains').

        A COLLECTED `replace_scope` write widens instead, to every non-key
        column minus the volatile ones, INDEPENDENT of `present`. That is
        not a preference, it is what makes the diff equivalent to the
        delete-plus-insert it replaces: the declared `update_columns` on
        these tables are partial (`financial_facts` updates `("value",)`,
        `sec_filing_exhibits` `("url",)`), so a plain upsert would leave
        every other column at the value the deleted row had. A column the
        rows omit entirely takes the table default through `excluded.col`,
        and one `align_rows` had to fill takes the explicit NULL it was
        filled with -- both exactly what delete-plus-insert produces,
        server defaults such as `first_seen_at` resetting included.
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

        This is what makes "a row came back" mean "the row changed", with no
        second read to find out: a `DO UPDATE ... WHERE` whose predicate is
        false returns nothing at all (measured --
        docs/measurements/database.md).

        Two kinds of term. Comparable columns are compared row-wise, which is
        one `IS DISTINCT FROM` rather than one per column and gives NULL the
        same treatment the rest of the codebase gives it. Monotonic columns
        get their own term, because `GREATEST` is what decides whether they
        move: comparing them directly would fire on every downward report the
        source makes, which is exactly what `GREATEST` exists to absorb.

        Volatile columns are in neither set. A row whose only difference is
        `fetched_at` did not change, and publishing it would have every
        consumer rewrite its mirror daily. They are still written -- see
        `_touch_volatile`.

        Returns None when both sets are empty, which is precisely the hash
        gate's `UNCHANGED_UPDATE_COLUMNS = ("fetched_at",)` write. No
        predicate, no `RETURNING`, no event: the statement is today's.
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

        `present` is derived from all rows and passed in; computing it
        per chunk would undo what align_rows just did.

        index_elements must match a unique constraint exactly as a set --
        a subset and a superset both raise "there is no unique or
        exclusion constraint matching the ON CONFLICT specification"
        (order does not matter). test_persistence_contract keeps
        key_columns honest.
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

        What came back is exactly what changed: the predicate suppresses the
        rows that did not, and `DO NOTHING` returns only what it inserted.
        There is no matching back against the proposed rows and no second
        read.
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

        A first sync or `--full-refresh` writes ~20,000 bars per symbol;
        across a 4,500-symbol universe that is on the order of 10^8 row
        events to say "the history is here". Steady-state daily writes
        (~390 one-minute bars) stay row-level, and so do repairs.
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
        """Writes the volatile columns the predicate stopped the upsert from writing.

        Without this they would freeze. The predicate means a row whose
        comparable columns are unchanged is not updated AT ALL, so
        `fetched_at` -- which `HashGate` reads as "last verified at" -- and
        `as_of_date` -- which `prune_asof` reads -- would keep the value they
        had on the first write, and both readers would draw the wrong
        conclusion from it.

        Only the rows the `RETURNING` did NOT report need it: the ones it did
        report were updated, volatile columns included. Each row is given its
        OWN proposed values, which is why this is `FROM (VALUES ...)` and not
        one UPDATE per distinct value.

        It emits no event, which is the whole point: the row did not change.

        Skipped entirely when a volatile column is part of the key -- the
        `*_history` snapshots, where `fetched_at` identifies the row rather
        than dating it, so touching it would move the row instead.
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

        `scope_columns` defines it, defaulting to ("symbol",):
        `company_officers` works per symbol, `financial_facts` per
        (symbol, statement, freq, period_end). `scope_values` is used when
        the write gives it -- the hash-gated datasets do, because their rows
        can be empty while the scope is not.
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

        Without a collector this is one DELETE, exactly as it has always
        been: the scope goes, the incoming rows are inserted, and which rows
        survived is nobody's question.

        With one it becomes a diff, because "delete everything and insert it
        back" would publish every row of every `replace_scope` table as an
        insert on every sync -- ten dataset modules, and the whole point of
        the predicate on the upsert path is not to do that.
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

        With no incoming rows `incoming` is empty and the whole scope is
        deleted, which is what today's single DELETE does. A key that is
        removed and re-added in one write cannot happen: it is in
        `incoming`, so it is never in the delete set.
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
        #
        # The key list MUST be deduplicated. It is chunked, and one IN
        # list collapses its own duplicates while two chunks do not: the
        # same key appearing in both chunks was counted twice, inflating
        # `verified` back up to `attempted` and reporting `ok` for a write
        # that stored fewer rows than it claimed.
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
