"""INSERT_CHUNK: chunking large TableWrites.

Does not touch a database: a fake that records calls stands in for
Session, so "how many INSERTs were produced" and "does every chunk carry
the same column set" can be answered with no network and no DB.

The one real trap in chunking is align_rows ORDER: the column set can vary
row to row (a non-fund symbol has no 'Capital Gains'), and if align_rows
runs AFTER chunking, each chunk ends up with a different column set and a
different update map.
"""

from __future__ import annotations

from typing import Any

import pytest

from yfin.storage.contracts import TableWrite
from yfin.storage.persistence import INSERT_CHUNK, PostgresRowWriter, dedupe_rows


class RecordingSession:
    """Fake Session that records execute() calls."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        self.statements.append(statement)
        return _FakeResult()


class _FakeResult:
    def scalar_one(self) -> int:
        return 0


def _write(rows: list[dict[str, Any]]) -> TableWrite:
    return TableWrite(
        table="price_bars",
        rows=rows,
        key_columns=("symbol", "bar_interval", "ts_utc"),
        update_columns=("close", "volume"),
    )


def _bar_rows(count: int, *, drop_volume_after: int | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i in range(count):
        row: dict[str, Any] = {
            "symbol": "AAPL",
            "bar_interval": "1m",
            "ts_utc": f"2026-09-02 14:{i:02d}:00",
            "local_date": "2026-09-02",
            "close": 100 + i,
        }
        if drop_volume_after is None or i < drop_volume_after:
            row["volume"] = 1000 + i
        rows.append(row)
    return rows


def _insert_statements(session: RecordingSession) -> list[Any]:
    """Filters out SELECT verification queries and returns only INSERTs."""
    return [s for s in session.statements if s.__class__.__name__ == "Insert"]


def test_large_write_is_split_into_chunks() -> None:
    session = RecordingSession()
    writer = PostgresRowWriter(session)  # type: ignore[arg-type]

    rows = _bar_rows(INSERT_CHUNK * 2 + 1)
    writer.write(_write(rows))

    inserts = _insert_statements(session)
    assert len(inserts) == 3, f"expected 3 chunks, got {len(inserts)} INSERTs"
    counts = [len(stmt.compile().params) // len(rows[0]) for stmt in inserts]
    assert sum(counts) == len(rows)


def test_small_write_produces_single_insert() -> None:
    session = RecordingSession()
    writer = PostgresRowWriter(session)  # type: ignore[arg-type]

    writer.write(_write(_bar_rows(3)))

    assert len(_insert_statements(session)) == 1


def test_every_chunk_carries_the_same_column_set() -> None:
    """align_rows must be applied before chunking.

    The first half has `volume`, the second half does not. If align_rows
    runs first, both chunks carry `volume` (None in the second); if it
    runs after, the second chunk never sees it and it silently drops out
    of the update scope.
    """
    session = RecordingSession()
    writer = PostgresRowWriter(session)  # type: ignore[arg-type]

    rows = _bar_rows(INSERT_CHUNK + 2, drop_volume_after=INSERT_CHUNK)
    writer.write(_write(rows))

    inserts = _insert_statements(session)
    assert len(inserts) == 2

    # The columns each chunk actually writes: compiled parameter names take
    # the form "close_m0", "volume_m1"; the part before the last underscore
    # segment is the column name.
    def written_columns(stmt: Any) -> set[str]:
        return {name.rsplit("_m", 1)[0] for name in stmt.compile().params}

    assert written_columns(inserts[0]) == written_columns(inserts[1])
    assert "volume" in written_columns(inserts[1]), (
        "second chunk never saw volume: align_rows ran AFTER chunking"
    )
    # and it must stay in the update scope too. In PostgreSQL that scope is
    # `OnConflictDoUpdate.update_values_to_set`, the counterpart of MySQL's
    # `OnDuplicateClause.update` dict.
    for stmt in inserts:
        clause = stmt._post_values_clause
        updated = {name for name, _ in clause.update_values_to_set}
        assert "volume" in updated


def test_empty_write_produces_no_insert() -> None:
    session = RecordingSession()
    writer = PostgresRowWriter(session)  # type: ignore[arg-type]

    assert writer.write(_write([])) == 0
    assert _insert_statements(session) == []


@pytest.mark.parametrize("size", [1, INSERT_CHUNK - 1, INSERT_CHUNK, INSERT_CHUNK + 1])
def test_chunk_boundaries(size: int) -> None:
    session = RecordingSession()
    writer = PostgresRowWriter(session)  # type: ignore[arg-type]

    writer.write(_write(_bar_rows(size)))

    expected = (size + INSERT_CHUNK - 1) // INSERT_CHUNK
    assert len(_insert_statements(session)) == expected


class TestDedupeRows:
    """PostgreSQL's `ON CONFLICT DO UPDATE` cannot touch the same row twice
    in one statement (ERROR 21000, "cannot affect row a second time").
    MySQL's `ON DUPLICATE KEY UPDATE` swallowed this without complaint, so
    most datasets have no within-chunk uniqueness guarantee.
    """

    def test_last_wins_for_repeated_key(self) -> None:
        rows = [
            {"symbol": "AAPL", "session_date": "2026-01-02", "close": 1},
            {"symbol": "AAPL", "session_date": "2026-01-02", "close": 2},
            {"symbol": "MSFT", "session_date": "2026-01-02", "close": 9},
        ]
        out = dedupe_rows(rows, ("symbol", "session_date"), ())
        assert out == [
            {"symbol": "AAPL", "session_date": "2026-01-02", "close": 2},
            {"symbol": "MSFT", "session_date": "2026-01-02", "close": 9},
        ]

    def test_preserves_first_seen_order(self) -> None:
        rows = [{"k": "b", "v": 1}, {"k": "a", "v": 1}, {"k": "b", "v": 2}]
        out = dedupe_rows(rows, ("k",), ())
        assert [r["k"] for r in out] == ["b", "a"]

    def test_monotonic_column_takes_group_max(self) -> None:
        """GREATEST compares the new row only against the existing DB row,
        not two rows in the same batch. Plain "last wins" would roll back
        monotonicity within a chunk."""
        rows = [
            {"symbol": "AAPL", "session_date": "2026-01-02", "is_repaired": True},
            {"symbol": "AAPL", "session_date": "2026-01-02", "is_repaired": False},
        ]
        out = dedupe_rows(rows, ("symbol", "session_date"), ("is_repaired",))
        assert len(out) == 1
        assert out[0]["is_repaired"] is True

    def test_none_never_beats_a_value_in_monotonic_column(self) -> None:
        rows = [{"k": "a", "m": 5}, {"k": "a", "m": None}]
        out = dedupe_rows(rows, ("k",), ("m",))
        assert out[0]["m"] == 5

    def test_untouched_when_keys_are_unique(self) -> None:
        rows = [{"k": "a"}, {"k": "b"}]
        assert dedupe_rows(rows, ("k",), ()) is rows
