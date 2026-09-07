"""What the writer's INSERT compiles to, with a collector and without one.

The change-events design adds three things to the upsert -- `RETURNING *`,
a distinctness predicate and a volatile touch -- and every one of them is a
change to the statement that writes 68 tables. So the first thing this file
does is lock in that WITHOUT a collector nothing moved: the three shapes
compile to the same text they compiled to before, compared as strings.

The rest covers the shapes a collector produces, including the two that
must NOT acquire a predicate: an infrastructure table, and a write whose
only updatable column is volatile.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa

from yfin.models.base import Base
from yfin.storage.changes import ChangeCollector, ChangeContext
from yfin.storage.contracts import TableWrite
from yfin.storage.persistence import PostgresRowWriter

PG = sa.dialects.postgresql.dialect()


def _sql(write: TableWrite, *, collector: ChangeCollector | None) -> str:
    """The statement one chunk of a write compiles to."""
    writer = PostgresRowWriter(_FakeSession(), collector=collector)
    table = Base.metadata.tables[write.table]
    rows = list(write.rows)
    statement = writer._insert_stmt(table, rows, write, set(rows[0]))
    return str(statement.compile(dialect=PG))


class _FakeSession:
    """`_insert_stmt` builds a statement; it never touches the session."""

    def execute(self, *_: Any, **__: Any) -> Any:  # pragma: no cover - not called
        raise AssertionError("_insert_stmt must not execute anything")


def _collector() -> ChangeCollector:
    return ChangeCollector(ChangeContext(run_id=1, range_threshold=1000))


def _fact_write(**overrides: Any) -> TableWrite:
    """A plain `DO UPDATE` write against a routed table."""
    defaults: dict[str, Any] = {
        "table": "financial_facts",
        "rows": [
            {
                "symbol": "AAPL",
                "statement": "income",
                "freq": "annual",
                "period_end": "2025-09-27",
                "item_key": "TotalRevenue",
                "value": 1,
            }
        ],
        "key_columns": ("symbol", "statement", "freq", "period_end", "item_key"),
        "update_columns": ("value",),
    }
    defaults.update(overrides)
    return TableWrite(**defaults)


def _target_write(**overrides: Any) -> TableWrite:
    """A write against a table that has both several comparable columns and
    a volatile one, which `financial_facts` does not."""
    defaults: dict[str, Any] = {
        "table": "analyst_price_targets",
        "rows": [
            {
                "symbol": "AAPL",
                "as_of_date": "2026-09-07",
                "current": 1,
                "low": 2,
                "fetched_at": "2026-09-07T00:00:00Z",
            }
        ],
        "key_columns": ("symbol", "as_of_date"),
        "update_columns": ("current", "low"),
    }
    defaults.update(overrides)
    return TableWrite(**defaults)


class TestNothingMovesWithoutACollector:
    """The regression lock. 68 tables go through this statement."""

    def test_do_update_is_unchanged(self) -> None:
        sql = _sql(_fact_write(), collector=None)
        assert "RETURNING" not in sql
        assert "IS DISTINCT FROM" not in sql
        assert sql.endswith("DO UPDATE SET value = excluded.value")

    def test_do_nothing_is_unchanged(self) -> None:
        """An empty update map still means 'insert, leave the rest alone'."""
        sql = _sql(_fact_write(update_columns=()), collector=None)
        assert "DO NOTHING" in sql
        assert "RETURNING" not in sql

    def test_the_guarded_shape_is_unchanged(self) -> None:
        write = TableWrite(
            table="live_quotes",
            rows=[{"symbol": "AAPL", "ts_utc": "2026-09-07T00:00:00Z", "price": 1}],
            key_columns=("symbol",),
            update_columns=("price",),
            guard_column="ts_utc",
        )
        sql = _sql(write, collector=None)
        assert "WHERE excluded.ts_utc > live_quotes.ts_utc" in sql
        assert "RETURNING" not in sql

    def test_the_monotonic_shape_is_unchanged(self) -> None:
        write = _fact_write(update_columns=("value",), monotonic_columns=("value",))
        sql = _sql(write, collector=None)
        assert "greatest(financial_facts.value, excluded.value)" in sql
        assert "RETURNING" not in sql


class TestWithACollector:
    def test_the_row_comes_back_with_its_branch(self) -> None:
        """`xmax = 0` is a system column, so it is a literal rather than a
        model column -- `Base.metadata` has no such attribute."""
        sql = _sql(_fact_write(), collector=_collector())
        assert sql.endswith("RETURNING *, xmax = 0 AS inserted")

    def test_a_comparable_column_gets_a_distinctness_predicate(self) -> None:
        """Without it every row would come back on every run, and 'a row was
        returned' would stop meaning 'the row changed'."""
        sql = _sql(_fact_write(), collector=_collector())
        assert "(financial_facts.value) IS DISTINCT FROM (excluded.value)" in sql

    def test_several_comparable_columns_compare_row_wise(self) -> None:
        """One `IS DISTINCT FROM` over a row constructor, not one per column:
        fewer terms, and NULL is treated the way the rest of the codebase
        treats it."""
        sql = _sql(_target_write(), collector=_collector())
        assert (
            "(analyst_price_targets.current, analyst_price_targets.low) "
            "IS DISTINCT FROM (excluded.current, excluded.low)" in sql
        )

    def test_a_monotonic_column_gets_a_greatest_term(self) -> None:
        """A monotonic column `GREATEST` would raise is a change and must
        both update and emit; one it would leave alone is neither."""
        write = _fact_write(monotonic_columns=("value",))
        sql = _sql(write, collector=_collector())
        assert (
            "greatest(financial_facts.value, excluded.value) IS DISTINCT FROM "
            "financial_facts.value" in sql
        )

    def test_a_monotonic_column_is_not_also_compared_directly(self) -> None:
        """It would fire on every downward report the source makes, which is
        exactly what `GREATEST` exists to absorb."""
        write = _fact_write(monotonic_columns=("value",))
        sql = _sql(write, collector=_collector())
        assert "(financial_facts.value) IS DISTINCT FROM (excluded.value)" not in sql

    def test_do_nothing_still_returns_its_inserts(self) -> None:
        """The shape is unchanged -- existing rows are left alone -- but the
        rows it did insert are events."""
        sql = _sql(_fact_write(update_columns=()), collector=_collector())
        assert "DO NOTHING" in sql
        assert "RETURNING *, xmax = 0 AS inserted" in sql
        assert "IS DISTINCT FROM" not in sql


class TestTheTwoShapesThatStayAsTheyAre:
    def test_an_infrastructure_table_gets_todays_statement(self) -> None:
        """The gate rows carry `GATE_UPDATE_COLUMNS` and are never published,
        so a predicate and a `RETURNING *` would be paid for nothing."""
        write = TableWrite(
            table="asof_state",
            rows=[
                {
                    "symbol": "AAPL",
                    "dataset": "recommendations",
                    "as_of_date": "2026-09-07",
                    "content_hash": "abc",
                }
            ],
            key_columns=("symbol", "dataset"),
            update_columns=("as_of_date", "content_hash"),
        )
        sql = _sql(write, collector=_collector())
        assert "RETURNING" not in sql
        assert "IS DISTINCT FROM" not in sql

    def test_an_all_volatile_update_map_gets_no_predicate(self) -> None:
        """`UNCHANGED_UPDATE_COLUMNS = ("fetched_at",)`: the hash gate
        writing 'we checked' is not a change anyone can apply, and the row
        still has to be written or `HashGate` would read a frozen date."""
        sql = _sql(_target_write(update_columns=("fetched_at",)), collector=_collector())
        assert "RETURNING" not in sql
        assert "IS DISTINCT FROM" not in sql
        assert "DO UPDATE SET fetched_at = excluded.fetched_at" in sql


class TestGuardsAreRefused:
    def test_a_guard_column_on_a_published_table_raises(self) -> None:
        """A guard-rejected row and an unchanged row are indistinguishable in
        `RETURNING`, so the event would claim a write that did not happen.

        Defensive rather than reachable today: the only guard in the codebase
        is the stream writer's on `live_quotes`, which is infrastructure and
        never collected. This is the check that would catch a pipeline table
        growing one.
        """
        with pytest.raises(ValueError, match="guard_column"):
            _sql(_target_write(guard_column="as_of_date"), collector=_collector())

    def test_a_guard_on_an_infrastructure_table_is_still_fine(self) -> None:
        """`live_quotes` is where the only guard lives, and it is never
        collected -- so the stream writer keeps working unchanged even with a
        collector in the process."""
        write = TableWrite(
            table="live_quotes",
            rows=[{"symbol": "AAPL", "ts_utc": "2026-09-07T00:00:00Z", "price": 1}],
            key_columns=("symbol",),
            update_columns=("price",),
            guard_column="ts_utc",
        )
        assert "RETURNING" not in _sql(write, collector=_collector())
