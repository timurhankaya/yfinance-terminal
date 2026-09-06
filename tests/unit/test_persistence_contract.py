"""Verifies dataset write logic is independent of the database.

None of the tests in this file touch a real database. That is possible
because `Dataset.upsert` now depends on the `RowWriter` protocol rather
than a SQLAlchemy Session (SRP/DIP separation).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from helpers import load_fixture
from yfin.datasets import SYMBOL_DATASETS as REGISTRY
from yfin.datasets.base import NormalizedResult
from yfin.datasets.payloads import FastInfoPayload, InfoPayload
from yfin.storage.contracts import RowWriter, TableWrite, WriteStats, apply_write


class FakeWriter:
    """In-memory implementation of the RowWriter protocol."""

    def __init__(self, hashes: dict[tuple[str, str], str] | None = None) -> None:
        self.written: list[TableWrite] = []
        self.hashes = hashes or {}
        self.known: set[str] = set()

    def write(self, write: TableWrite) -> int:
        self.written.append(write)
        return len(write.rows)  # every row is treated as verified

    def current_hash(self, table: str, key: Mapping[str, Any]) -> str | None:
        return self.hashes.get((table, "|".join(str(v) for v in key.values())))

    def known_symbols(self, candidates: set[str]) -> set[str]:
        return candidates & self.known

    def rows_for(self, table: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for write in self.written:
            if write.table == table:
                out.extend(write.rows)
        return out


def _protocol_check(writer: FakeWriter) -> RowWriter:
    """Whether FakeWriter really satisfies RowWriter (mypy + runtime)."""
    return writer


def test_fake_writer_satisfies_the_protocol() -> None:
    assert _protocol_check(FakeWriter()) is not None


class TestSnapshotSkipLogic:
    """If content_hash is unchanged, _history is not written; this is 'skipped'."""

    @staticmethod
    def _result() -> tuple[NormalizedResult, str]:
        payload = InfoPayload(load_fixture("AAPL", "info"), datetime(2026, 9, 4))
        result = REGISTRY["info"].normalize(payload, "AAPL")
        digest = next(w.rows[0]["content_hash"] for w in result.writes if w.table == "ticker_info")
        return result, digest

    def test_first_write_goes_to_history(self) -> None:
        result, _ = self._result()
        writer = FakeWriter()
        stats = REGISTRY["info"].upsert(writer, result)
        assert stats.attempted["ticker_info_history"] == 1
        assert stats.skipped.get("ticker_info_history", 0) == 0

    def test_unchanged_hash_is_skipped_not_written(self) -> None:
        result, digest = self._result()
        writer = FakeWriter(hashes={("ticker_info", "AAPL"): digest})
        stats = REGISTRY["info"].upsert(writer, result)
        assert stats.skipped["ticker_info_history"] == 1
        assert stats.attempted["ticker_info_history"] == 0
        # The current snapshot is still written
        assert stats.attempted["ticker_info"] == 1

    def test_changed_hash_is_written(self) -> None:
        result, _ = self._result()
        writer = FakeWriter(hashes={("ticker_info", "AAPL"): "eski" * 16})
        stats = REGISTRY["info"].upsert(writer, result)
        assert stats.attempted["ticker_info_history"] == 1
        assert stats.skipped.get("ticker_info_history", 0) == 0

    def test_hash_is_read_before_snapshot_is_updated(self) -> None:
        """If the snapshot were written first, the comparison would always
        match and _history would never get a new row."""
        result, digest = self._result()
        order: list[str] = []

        class OrderingWriter(FakeWriter):
            def current_hash(self, table: str, key: Mapping[str, Any]) -> str | None:
                order.append(f"read:{table}")
                return super().current_hash(table, key)

            def write(self, write: TableWrite) -> int:
                order.append(f"write:{write.table}")
                return super().write(write)

        REGISTRY["info"].upsert(OrderingWriter(), result)
        assert order.index("read:ticker_info") < order.index("write:ticker_info")


class TestNewsKnownFlag:
    def test_is_known_reflects_symbol_universe(self) -> None:
        result = REGISTRY["news"].normalize(load_fixture("AAPL", "news"), "AAPL")
        writer = FakeWriter()
        writer.known = {"AAPL"}
        REGISTRY["news"].upsert(writer, result)

        links = writer.rows_for("news_symbols")
        assert links
        assert all(r["is_known"] is (r["symbol"] == "AAPL") for r in links)

    def test_out_of_universe_symbol_does_not_raise(self) -> None:
        """news_symbols.symbol has no FK; a symbol outside the universe must
        not drop that symbol's transaction."""
        result = REGISTRY["news"].normalize(load_fixture("SPY", "news"), "SPY")
        writer = FakeWriter()  # no symbol is known
        stats = REGISTRY["news"].upsert(writer, result)
        assert stats.attempted["news_symbols"] > 0
        assert all(r["is_known"] is False for r in writer.rows_for("news_symbols"))


class TestDefaultUpsert:
    def test_empty_write_is_recorded_as_zero(self) -> None:
        writer = FakeWriter()
        stats = WriteStats()
        apply_write(
            writer,
            TableWrite(table="dividends", rows=[], key_columns=("symbol",), update_columns=()),
            stats,
        )
        assert stats.attempted["dividends"] == 0
        assert stats.verified["dividends"] == 0

    def test_fast_info_writes_two_tables(self) -> None:
        payload = FastInfoPayload(dict(load_fixture("SPY", "fast_info")), datetime(2026, 9, 4))
        result = REGISTRY["fast_info"].normalize(payload, "SPY")
        writer = FakeWriter()
        stats = REGISTRY["fast_info"].upsert(writer, result)
        assert set(stats.attempted) == {"ticker_fast_info", "ticker_fast_info_history"}


# --- key_columns <-> PK/UNIQUE invariant ------------------------------------


def _valid_key_sets(table_name: str) -> list[set[str]]:
    """Column sets that could be a table's `ON CONFLICT` target."""
    from yfin.models import Base

    table = Base.metadata.tables[table_name]
    out = [{c.name for c in table.primary_key.columns}]
    for constraint in table.constraints:
        if constraint.__class__.__name__ == "UniqueConstraint":
            out.append({c.name for c in constraint.columns})
    for index in table.indexes:
        if index.unique:
            out.append({c.name for c in index.columns})
    return out


def _resolve_key_columns(node: Any, module: Any) -> tuple[str, ...] | None:
    """Resolve column names from an AST node; None if it cannot be resolved.

    A plain tuple, a reference to a module constant, and a starred
    expansion (`(*GATE_KEY, "item_key")`) are supported -- the latter two
    are where static scanning alone falls short.
    """
    import ast

    if isinstance(node, ast.Name):
        value = getattr(module, node.id, None)
        return tuple(value) if isinstance(value, tuple) else None
    if not isinstance(node, ast.Tuple):
        return None
    names: list[str] = []
    for element in node.elts:
        if isinstance(element, ast.Constant) and isinstance(element.value, str):
            names.append(element.value)
        elif isinstance(element, ast.Starred):
            inner = _resolve_key_columns(element.value, module)
            if inner is None:
                return None
            names.extend(inner)
        elif isinstance(element, ast.Name):
            value = getattr(module, element.id, None)
            if not isinstance(value, str):
                return None
            names.append(value)
        else:
            return None
    return tuple(names)


def test_every_declared_key_matches_a_real_unique_constraint() -> None:
    """`ON CONFLICT (cols)` requires an exact set match.

    Both a subset and a superset raise "there is no unique or exclusion
    constraint matching the ON CONFLICT specification" (order does not
    matter; measured). MySQL's `ON DUPLICATE KEY UPDATE` never checked its
    target, so this constraint is new.

    Without this invariant, a dataset added with the wrong `key_columns`
    would only blow up in production -- and only show up in tests if that
    dataset happened to have a fixture. This test needs no fixture.
    """
    import ast
    import importlib
    from pathlib import Path

    from yfin.models import Base

    root = Path(__file__).resolve().parents[2] / "src" / "yfin" / "datasets"
    problems: list[str] = []
    checked = 0
    unresolved: list[str] = []

    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root.parent.parent).with_suffix("")
        module = importlib.import_module(".".join(rel.parts))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "TableWrite"):
                continue
            kwargs = {kw.arg: kw.value for kw in node.keywords}
            table_node, key_node = kwargs.get("table"), kwargs.get("key_columns")
            if not isinstance(table_node, ast.Constant) or key_node is None:
                unresolved.append(f"{path.name}:{node.lineno}")
                continue
            keys = _resolve_key_columns(key_node, module)
            if keys is None:
                unresolved.append(f"{path.name}:{node.lineno}")
                continue
            table_name = str(table_node.value)
            checked += 1
            if table_name not in Base.metadata.tables:
                problems.append(f"{path.name}:{node.lineno} unknown table {table_name}")
            elif set(keys) not in _valid_key_sets(table_name):
                problems.append(
                    f"{path.name}:{node.lineno} {table_name} {keys} "
                    f"matches no PK/UNIQUE constraint"
                )

    assert not problems, problems
    # Measured: 38 calls resolve statically, 34 do not (a tuple coming from
    # a variable, a conditional branch, a dynamic table name). The
    # thresholds are not loose: if coverage drops or the unresolved count
    # grows, this goes red -- otherwise the invariant would weaken silently.
    #
    # One of the unresolved calls is deliberate: `datasets/bars.py` picks
    # its table via `bars_table_for(interval)` (intraday -> price_bars,
    # 1wk/1mo -> periodic_bars). That call cannot be resolved statically;
    # instead, the test below proves the two tables share an identical PK,
    # so `key_columns` stays valid whichever branch routing takes.
    assert checked >= 38, f"number of audited calls DROPPED: {checked}"
    assert len(unresolved) <= 34, f"number of unresolved calls GREW: {unresolved}"


def test_both_bar_tables_share_the_same_primary_key() -> None:
    """The validity of `bars_table_for` routing relies on this.

    `datasets/bars.py` can write to two tables (price_bars / periodic_bars)
    with one `key_columns`. If their PKs diverged, `ON CONFLICT` would blow
    up on one branch with "no unique or exclusion constraint matching" --
    and only when that interval runs, i.e. late. Static scanning cannot
    resolve the dynamic table name, so the invariant is guarded here
    instead.
    """
    from yfin.models import Base, bars_table_for

    pk = {
        name: {c.name for c in Base.metadata.tables[name].primary_key.columns}
        for name in ("price_bars", "periodic_bars")
    }
    assert pk["price_bars"] == pk["periodic_bars"] == {"symbol", "bar_interval", "ts_utc"}
    assert bars_table_for("1m") == "price_bars"
    assert bars_table_for("1wk") == "periodic_bars"


def test_the_daily_interval_resolves_to_its_own_table() -> None:
    """`1d` has a different shape from the bar tables -- keyed on the
    exchange session date, carrying adj_close, with no bar_interval -- so
    it lives in price_history and the resolver has to say so."""
    from yfin.models import bars_table_for

    assert bars_table_for("1d") == "price_history"


def test_an_unknown_interval_RAISES_instead_of_guessing() -> None:
    """This used to fall through to periodic_bars for anything it did not
    recognise, so `bars_table_for("1d")` quietly named the wrong table:
    no error, no warning, just a query against a different primary key."""
    import pytest

    from yfin.models import bars_table_for

    with pytest.raises(ValueError, match="unknown interval"):
        bars_table_for("3mo")
