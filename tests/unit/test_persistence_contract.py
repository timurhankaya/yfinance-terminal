"""Dataset yazma mantiginin veritabanindan bagimsiz oldugunu dogrular.

Bu dosyadaki testler MySQL'e HIC dokunmaz. Mumkun olmasinin sebebi
`Dataset.upsert`'in artik SQLAlchemy Session'a degil `RowWriter`
protokoluene bagli olmasidir (SRP/DIP ayrimi).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from helpers import load_fixture
from yfin.datasets import SYMBOL_DATASETS as REGISTRY
from yfin.datasets.base import NormalizedResult, TableWrite, WriteStats
from yfin.datasets.payloads import FastInfoPayload, InfoPayload
from yfin.persistence import RowWriter, apply_write


class FakeWriter:
    """RowWriter protokolunun bellek ici uygulamasi."""

    def __init__(self, hashes: dict[tuple[str, str], str] | None = None) -> None:
        self.written: list[TableWrite] = []
        self.hashes = hashes or {}
        self.known: set[str] = set()

    def write(self, write: TableWrite) -> int:
        self.written.append(write)
        return len(write.rows)  # her satir dogrulanmis kabul edilir

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
    """FakeWriter gercekten RowWriter'i karsiliyor mu (mypy + calisma zamani)."""
    return writer


def test_fake_writer_satisfies_the_protocol() -> None:
    assert _protocol_check(FakeWriter()) is not None


class TestSnapshotSkipLogic:
    """content_hash degismediyse _history'ye yazilmaz; bu 'skipped'tir (S7.2)."""

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
        # Guncel snapshot yine de yazilir
        assert stats.attempted["ticker_info"] == 1

    def test_changed_hash_is_written(self) -> None:
        result, _ = self._result()
        writer = FakeWriter(hashes={("ticker_info", "AAPL"): "eski" * 16})
        stats = REGISTRY["info"].upsert(writer, result)
        assert stats.attempted["ticker_info_history"] == 1
        assert stats.skipped.get("ticker_info_history", 0) == 0

    def test_hash_is_read_before_snapshot_is_updated(self) -> None:
        """Snapshot once yazilsaydi karsilastirma her zaman esitlenir ve
        _history hicbir zaman yeni satir almazdi."""
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
        """news_symbols.symbol'da FK yoktur; evren disi sembol sembolun
        transaction'ini dusurmemelidir (S5.5)."""
        result = REGISTRY["news"].normalize(load_fixture("SPY", "news"), "SPY")
        writer = FakeWriter()  # hicbir sembol bilinmiyor
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


# --- key_columns <-> PK/UNIQUE invaryanti (PG S9.2) ------------------------


def _valid_key_sets(table_name: str) -> list[set[str]]:
    """Tablonun `ON CONFLICT` hedefi olabilecek kolon kumeleri."""
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
    """AST dugumunden kolon adlarini cozer; cozemezse None.

    Duz tuple, modul sabitine referans ve yildiz-acilimi
    (`(*GATE_KEY, "item_key")`) desteklenir -- son ikisi statik taramanin
    tek basina yetmedigi yerlerdi.
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
    """`ON CONFLICT (cols)` KUME OLARAK TAM ESLESME ister.

    Alt kume de ust kume de "there is no unique or exclusion constraint
    matching the ON CONFLICT specification" hatasi verir (sira
    onemsizdir; olculdu). MySQL `ON DUPLICATE KEY UPDATE` hedefi hic
    sormuyordu, yani bu kisit YENIDIR.

    Invaryant olmadan yanlis `key_columns` ile eklenen bir dataset ancak
    URETIMDE patlar -- ustelik yalnizca o dataset'in fixture'i varsa
    testlerde gorulurdu. Bu test fixture GEREKTIRMEZ.
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
                problems.append(f"{path.name}:{node.lineno} bilinmeyen tablo {table_name}")
            elif set(keys) not in _valid_key_sets(table_name):
                problems.append(
                    f"{path.name}:{node.lineno} {table_name} {keys} "
                    f"hicbir PK/UNIQUE ile eslesmiyor"
                )

    assert not problems, problems
    # Olculdu: 39 cagri statik olarak cozuluyor, 33'u cozulemiyor
    # (degiskenden gelen tuple, kosullu dal). Esikler GEVSEK DEGIL:
    # kapsam duserse ya da cozulemeyenler artarsa burasi kirmizi olur --
    # aksi halde invaryant sessizce zayiflardi.
    assert checked >= 39, f"denetlenen cagri sayisi DUSTU: {checked}"
    assert len(unresolved) <= 33, f"cozulemeyen cagri sayisi ARTTI: {unresolved}"
