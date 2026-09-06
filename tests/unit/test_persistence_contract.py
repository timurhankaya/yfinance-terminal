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
