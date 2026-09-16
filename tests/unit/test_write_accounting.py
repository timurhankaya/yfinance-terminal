"""Row accounting on the write path.

Three defects are pinned here, all of which reported success while
storing fewer rows than claimed, or failure on a correct write.
"""

from __future__ import annotations

from dataclasses import replace

from yfin.storage.contracts import TableWrite, distinct_key_count
from yfin.storage.persistence import MAX_BIND_PARAMS, dedupe_rows, insert_chunk_size


def _write(rows: list[dict[str, object]], **kw: object) -> TableWrite:
    base = TableWrite(
        table="t",
        rows=rows,
        key_columns=("a", "b"),
        update_columns=("v",),
    )
    return replace(base, **kw)  # type: ignore[arg-type]


class TestInsertChunkSize:
    def test_a_wide_table_stays_under_the_parameter_ceiling(self) -> None:
        """screen_quotes has 107 columns; 2000 rows asked for 214,000 bind
        parameters and every screener run died with OperationalError."""
        for columns in (1, 10, 92, 107, 191, 500):
            assert insert_chunk_size(columns) * columns <= MAX_BIND_PARAMS

    def test_a_narrow_table_is_still_capped_by_the_row_limit(self) -> None:
        assert insert_chunk_size(4) == 2000

    def test_never_returns_zero(self) -> None:
        """A table wider than the ceiling must still make progress, one row
        per statement, rather than loop forever on a zero-sized slice."""
        assert insert_chunk_size(MAX_BIND_PARAMS + 1) == 1
        assert insert_chunk_size(0) > 0


class TestDistinctKeyCount:
    def test_duplicate_keys_count_once(self) -> None:
        """The writer collapses same-key rows, so counting the raw list made
        `verified != attempted` and marked a correct write FAILED."""
        rows = [
            {"a": 1, "b": "x", "v": 1},
            {"a": 1, "b": "x", "v": 2},
            {"a": 2, "b": "x", "v": 3},
        ]
        write = _write(rows)
        assert distinct_key_count(write) == 2
        assert len(dedupe_rows(rows, write.key_columns, ())) == 2

    def test_it_agrees_with_dedupe_rows_on_every_shape(self) -> None:
        """These two must never disagree: one decides what is stored, the
        other decides what success looks like."""
        cases = [
            [],
            [{"a": 1, "b": "x"}],
            [{"a": 1, "b": "x"}, {"a": 1, "b": "y"}],
            [{"a": 1, "b": "x"}, {"a": 1, "b": "x"}, {"a": 1, "b": "x"}],
            [{"a": None, "b": "x"}, {"a": None, "b": "x"}],
        ]
        for rows in cases:
            write = _write(list(rows))
            assert distinct_key_count(write) == len(dedupe_rows(list(rows), ("a", "b"), ()))

    def test_without_key_columns_it_falls_back_to_the_row_count(self) -> None:
        """An empty key tuple would otherwise collapse every row into one."""
        write = _write([{"a": 1}, {"a": 2}], key_columns=())
        assert distinct_key_count(write) == 2


class TestTableWriteRebuilds:
    def test_replace_carries_monotonic_columns(self) -> None:
        """Rebuilding TableWrite must carry `monotonic_columns`, or
        GREATEST() becomes last-wins and is_repaired regresses to 0."""
        write = _write([{"a": 1, "b": "x"}], monotonic_columns=("is_repaired",))
        assert replace(write, rows=[]).monotonic_columns == ("is_repaired",)

    def test_replace_carries_scope_fields(self) -> None:
        write = _write([{"a": 1, "b": "x"}], scope_columns=("k",), scope_values=({"k": 1},))
        rebuilt = replace(write, rows=[])
        assert rebuilt.scope_columns == ("k",)
        assert rebuilt.scope_values == ({"k": 1},)
