"""AsOfDataset hash gate. Never touches a database."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from yfin.datasets.asof_base import GATE_TABLE, GATE_UPDATE_COLUMNS, AsOfDataset
from yfin.datasets.base import NormalizedResult, SyncContext
from yfin.storage.contracts import TableWrite

AS_OF = date(2026, 9, 4)
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 4, 18, 30, tzinfo=UTC)


class FakeWriter:
    def __init__(self, hashes: dict[tuple[str, str], str] | None = None) -> None:
        self.written: list[TableWrite] = []
        self.hashes = hashes or {}

    def write(self, write: TableWrite) -> int:
        self.written.append(write)
        return len(write.rows)

    def current_hash(self, table: str, key: Mapping[str, Any]) -> str | None:
        return self.hashes.get((table, "|".join(str(v) for v in key.values())))

    def known_symbols(self, candidates: set[str]) -> set[str]:
        return set()

    def rows_for(self, table: str) -> list[dict[str, Any]]:
        return [r for w in self.written if w.table == table for r in w.rows]

    def write_for(self, table: str) -> TableWrite:
        return next(w for w in self.written if w.table == table)


class Holders(AsOfDataset[None]):
    name = "institutional_holders"
    produces = ("institutional_holders", GATE_TABLE)
    gate_source_tables = ("institutional_holders",)

    def fetch(self, ctx: SyncContext) -> None:
        return None

    def normalize(self, raw: None, symbol: str) -> NormalizedResult:
        return NormalizedResult()


def _rows(*holders: str, fetched_at: datetime = NOW) -> list[dict[str, Any]]:
    return [
        {
            "symbol": "AAPL",
            "as_of_date": AS_OF,
            "holder_type": "institution",
            "holder": h,
            "shares": Decimal("10"),
            "fetched_at": fetched_at,
        }
        for h in holders
    ]


def _result(rows: list[dict[str, Any]]) -> NormalizedResult:
    return NormalizedResult(
        writes=[
            TableWrite(
                table="institutional_holders",
                rows=rows,
                key_columns=("symbol", "as_of_date", "holder_type", "holder"),
                update_columns=("shares", "fetched_at"),
                mode="replace_scope",
                scope_columns=("symbol", "as_of_date", "holder_type"),
                scope_values=(
                    {"symbol": "AAPL", "as_of_date": AS_OF, "holder_type": "institution"},
                ),
            )
        ]
    )


def _hash(rows: list[dict[str, Any]]) -> str:
    return Holders().content_hash(_result(rows))


# --- hash definition ---------------------------------------------------


def test_hash_ignores_fetched_at() -> None:
    """If VOLATILE_COLUMNS narrows, the mechanism silently stops working
    entirely: every row gets rewritten every day and nobody notices."""
    assert _hash(_rows("Vanguard", fetched_at=NOW)) == _hash(
        _rows("Vanguard", fetched_at=LATER)
    )


def test_hash_ignores_as_of_date() -> None:
    rows = _rows("Vanguard")
    other = [{**r, "as_of_date": date(2026, 9, 5)} for r in rows]
    assert _hash(rows) == _hash(other)


def test_hash_ignores_row_order() -> None:
    """canonical_json sorts only dict keys; list order is preserved. If
    Yahoo reorders its 'top 10' list, the hash would change with identical
    content -- the exact opposite of why this mechanism exists."""
    assert _hash(_rows("Vanguard", "BlackRock")) == _hash(_rows("BlackRock", "Vanguard"))


def test_hash_changes_with_content() -> None:
    assert _hash(_rows("Vanguard")) != _hash(_rows("BlackRock"))


def test_hash_is_stable_across_calls() -> None:
    assert _hash(_rows("Vanguard")) == _hash(_rows("Vanguard"))


# --- gate behavior -------------------------------------------------------


def test_empty_result_writes_nothing_at_all() -> None:
    """No gate row is written for an empty result; otherwise a dead row
    would accumulate for every non-fund symbol, and first_seen_at would
    drift to mean "first time it came back empty"."""
    writer = FakeWriter()
    stats = Holders().upsert(writer, NormalizedResult())
    assert writer.written == []
    assert stats.attempted == {}


def test_first_run_writes_data_and_gate() -> None:
    writer = FakeWriter()
    stats = Holders().upsert(writer, _result(_rows("Vanguard")))
    assert len(writer.rows_for("institutional_holders")) == 1
    gate = writer.rows_for(GATE_TABLE)
    assert len(gate) == 1
    assert gate[0]["symbol"] == "AAPL"
    assert gate[0]["dataset"] == "institutional_holders"
    assert gate[0]["row_count"] == 1
    assert gate[0]["first_seen_at"] == NOW
    assert stats.attempted["institutional_holders"] == 1


def test_gate_update_columns_exclude_first_seen_at() -> None:
    """If the update scope included first_seen_at, the "written only on
    the first INSERT" rule would break."""
    writer = FakeWriter()
    Holders().upsert(writer, _result(_rows("Vanguard")))
    assert "first_seen_at" not in writer.write_for(GATE_TABLE).update_columns


def test_unchanged_hash_skips_data_but_touches_gate() -> None:
    """The gate row is always written; if the hash matches, only fetched_at
    is updated -- so fetched_at means "time of last verification" (the
    principle hash_gated.py establishes)."""
    rows = _rows("Vanguard")
    writer = FakeWriter({(GATE_TABLE, "AAPL|institutional_holders"): _hash(rows)})
    stats = Holders().upsert(writer, _result(rows))

    assert writer.rows_for("institutional_holders") == []
    assert stats.skipped["institutional_holders"] == 1
    assert "institutional_holders" not in stats.attempted

    gate_write = writer.write_for(GATE_TABLE)
    assert gate_write.update_columns == ("fetched_at",)
    assert gate_write.rows[0]["fetched_at"] == NOW


def test_changed_hash_rewrites_everything() -> None:
    writer = FakeWriter({(GATE_TABLE, "AAPL|institutional_holders"): "eski-hash"})
    stats = Holders().upsert(writer, _result(_rows("Vanguard")))
    assert len(writer.rows_for("institutional_holders")) == 1
    assert stats.attempted["institutional_holders"] == 1
    assert stats.skipped == {}


def test_empty_child_table_stays_empty_not_skipped() -> None:
    """In a multi-table dataset, a target with an empty TableWrite stays
    EMPTY; skipped is written only for tables that carry rows. For BND,
    fund_top_holdings has 0 rows while its sibling tables get skipped."""
    rows = _rows("Vanguard")
    result = NormalizedResult(
        writes=[
            *_result(rows).writes,
            TableWrite(
                table="fund_top_holdings",
                rows=[],
                key_columns=("symbol", "as_of_date", "holding_symbol"),
                update_columns=(),
            ),
        ]
    )
    writer = FakeWriter(
        {(GATE_TABLE, "AAPL|institutional_holders"): Holders().content_hash(result)}
    )
    stats = Holders().upsert(writer, result)
    assert stats.skipped == {"institutional_holders": 1}
    assert "fund_top_holdings" not in stats.skipped


def test_gate_table_is_declared_in_produces() -> None:
    """produces's contract is 'the table names it writes to', and
    test_registry requires every entry to exist in the metadata."""
    assert GATE_TABLE in Holders.produces


def test_gate_row_is_counted_in_stats() -> None:
    """The gate row is counted in the stats; otherwise the success path
    would produce no asof_state audit row while the failure path would
    (an asymmetry)."""
    writer = FakeWriter()
    stats = Holders().upsert(writer, _result(_rows("Vanguard")))
    assert stats.attempted[GATE_TABLE] == 1
    assert stats.verified[GATE_TABLE] == 1
    assert GATE_TABLE in stats.tables()


def test_gate_row_is_counted_even_when_data_is_skipped() -> None:
    """Even when the hash matches, the gate is verified: "this symbol was
    checked today"."""
    rows = _rows("Vanguard")
    writer = FakeWriter({(GATE_TABLE, "AAPL|institutional_holders"): _hash(rows)})
    stats = Holders().upsert(writer, _result(rows))
    assert stats.attempted[GATE_TABLE] == 1
    assert stats.skipped["institutional_holders"] == 1


class TestGateSource:
    """The gate row's stamp has a declared origin, not a positional one."""

    def test_the_declared_table_is_used_even_when_another_comes_first(self) -> None:
        """The bug this replaced.

        `_gate_write` took the first row in `writes` order, so a dataset
        that put another table first produced its gate row from the wrong
        one -- a wrong `as_of_date` written silently, after which the gate
        compares against it forever and reports "unchanged".
        """
        dataset = Holders()
        result = NormalizedResult(
            writes=[
                TableWrite(
                    table="some_other_table",
                    rows=[
                        {
                            "symbol": "AAPL",
                            "as_of_date": date(1999, 1, 1),
                            "fetched_at": LATER,
                        }
                    ],
                    key_columns=("symbol",),
                    update_columns=(),
                ),
                *_result(_rows("Vanguard")).writes,
            ]
        )
        writer = FakeWriter()
        dataset.upsert(writer, result)
        gate = writer.rows_for(GATE_TABLE)[0]
        assert gate["as_of_date"] == AS_OF
        assert gate["fetched_at"] == NOW

    def test_a_declaration_naming_no_populated_table_raises(self) -> None:
        """Louder than the alternative, and deliberately so: falling back to
        whatever row exists is the silent corruption above."""
        class Misdeclared(Holders):
            name = "misdeclared"
            gate_source_tables = ("a_table_it_never_writes",)

        with pytest.raises(ValueError, match="gate source tables"):
            Misdeclared().upsert(FakeWriter(), _result(_rows("Vanguard")))

    def test_a_concrete_gated_dataset_cannot_omit_the_declaration(self) -> None:
        """Import time, not first write: the alternative surfaces on the
        first symbol of a live run, after the Yahoo call is paid for."""
        with pytest.raises(TypeError, match="gate_source_tables"):

            class Undeclared(AsOfDataset[None]):
                name = "undeclared"
                produces = ("undeclared", GATE_TABLE)

                def fetch(self, ctx: SyncContext) -> None:
                    return None

                def normalize(self, raw: None, symbol: str) -> NormalizedResult:
                    return NormalizedResult()


def test_every_registered_asof_dataset_declares_tables_it_writes() -> None:
    """A source naming a table the dataset does not write is a declaration
    that fails on every single run of it."""
    import yfin.datasets  # noqa: F401  - registers everything
    from yfin.datasets.asof_base import AsOfGate
    from yfin.datasets.registry import DOMAIN_DATASETS, SYMBOL_DATASETS

    seen = 0
    for registry in (SYMBOL_DATASETS, DOMAIN_DATASETS):
        for name in registry:
            dataset = registry[name]
            if not isinstance(dataset, AsOfGate):
                continue
            seen += 1
            assert dataset.gate_source_tables, name
            for table in dataset.gate_source_tables:
                assert table in dataset.produces, (name, table)
                assert table != dataset.asof_gate_table, (name, table)
    assert seen >= 15


def test_full_refresh_writes_the_data_even_when_the_hash_matches() -> None:
    """What `--full-refresh` could not do before.

    The flag only zeroed the watermark, and `asof_base` never looked at it:
    a symbol whose data rows were lost while its `asof_state` row survived
    refetched from Yahoo, matched the stored hash, reported `skipped`, and
    wrote nothing -- run after run, with the flag on.
    """
    rows = _rows("Vanguard")
    writer = FakeWriter({(GATE_TABLE, "AAPL|institutional_holders"): _hash(rows)})
    stats = Holders().upsert(writer, _result(rows), full_refresh=True)

    assert writer.rows_for("institutional_holders") == rows
    assert "institutional_holders" not in stats.skipped
    # The gate row is rewritten in full, not touched on `fetched_at` only:
    # a stale `as_of_date` left behind would survive the repair.
    assert writer.write_for(GATE_TABLE).update_columns == GATE_UPDATE_COLUMNS
