"""Market dataset and hash-gate tests."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from helpers import as_calendar_frame, load_fixture
from yfin.datasets import MARKET_DATASETS
from yfin.datasets.base import NormalizedResult
from yfin.datasets.hash_gated import HashGatedDataset
from yfin.datasets.market.base import MarketContext
from yfin.datasets.payloads import (
    CalendarFramePayload,
    MarketStatusPayload,
    MarketSummaryPayload,
)
from yfin.datasets.snapshot_base import snapshot_upsert
from yfin.storage.contracts import TableWrite

FETCHED_AT = datetime(2026, 9, 4, 10, 0, 0, 500000)
GATE_KEY = ("symbol", "statement", "freq", "period_end")


def _rows(result: Any, table: str) -> list[dict[str, Any]]:
    return [row for write in result.writes if write.table == table for row in write.rows]


def _market_fixture(name: str) -> Any:
    return load_fixture("_market", name)


class TestMarketStatus:
    def test_us_status_normalizes(self) -> None:
        raw = MarketStatusPayload(
            region="US", status=_market_fixture("market_status"), fetched_at=FETCHED_AT
        )
        result = MARKET_DATASETS["market_status"].normalize(raw)
        row = _rows(result, "market_status")[0]
        assert row["region"] == "US"
        assert row["market_id"] == "us"
        assert row["timezone_name"] == "America/New_York"
        assert row["gmt_offset"] is not None

    def test_none_status_is_empty_not_failure(self) -> None:
        """Status returns None (deterministically) in the 7 regions outside the US."""
        raw = MarketStatusPayload(region="EUROPE", status=None, fetched_at=FETCHED_AT)
        assert MARKET_DATASETS["market_status"].normalize(raw).is_empty

    def test_datetime_fields_serialize(self) -> None:
        """open/close are datetime objects; a plain json.dumps raises TypeError."""
        raw = MarketStatusPayload(
            region="US", status=_market_fixture("market_status"), fetched_at=FETCHED_AT
        )
        row = _rows(MARKET_DATASETS["market_status"].normalize(raw), "market_status")[0]
        assert "open" in row["raw_json"]


class TestMarketSummary:
    def _result(self) -> Any:
        raw = MarketSummaryPayload(
            region="US", summary=_market_fixture("market_summary"), fetched_at=FETCHED_AT
        )
        return MARKET_DATASETS["market_summary"].normalize(raw)

    def test_boards_become_rows(self) -> None:
        rows = _rows(self._result(), "market_summary")
        assert {r["board_code"] for r in rows} == {"CME", "CBT", "CXI", "CMX"}
        assert all(r["region"] == "US" for r in rows)

    def test_symbol_carried_without_fk(self) -> None:
        rows = _rows(self._result(), "market_summary")
        assert any(r["symbol"] for r in rows)
        assert all(r["is_known"] is False for r in rows)  # flagged during the upsert step

    def test_envelope_shape_raises_instead_of_writing_garbage(self) -> None:
        """On a parse failure, yfinance can return the raw envelope dict;
        without validating the shape, garbage would be written into the
        (region, board_code) PK."""
        raw = MarketSummaryPayload(
            region="US",
            summary={"marketSummaryResponse": {"result": []}},
            fetched_at=FETCHED_AT,
        )
        with pytest.raises(ValueError, match="unexpected market summary shape"):
            MARKET_DATASETS["market_summary"].normalize(raw)


class TestCalendars:
    def _result(self, dataset: str, fixture: str) -> Any:
        frame = as_calendar_frame(_market_fixture(fixture))
        return MARKET_DATASETS[dataset].normalize(
            CalendarFramePayload(frame=frame, fetched_at=FETCHED_AT)
        )

    def test_earnings_calendar(self) -> None:
        rows = _rows(self._result("earnings_calendar", "earnings_calendar"), "calendar_earnings")
        assert rows
        assert all(r["symbol"] == r["symbol"].upper() for r in rows)
        assert all(r["event_start_ts_utc"].tzinfo is UTC for r in rows)

    def test_economic_calendar_triple_key_is_unique(self) -> None:
        """Index (Event) is not unique (29 repeats in 100 rows); the
        three-column key was measured unique for 100/100."""
        raw = _market_fixture("economic_calendar")
        rows = _rows(self._result("economic_calendar", "economic_calendar"), "calendar_economic")
        keys = {(r["region"], r["event_time_utc"], r["event_name"]) for r in rows}
        assert len(keys) == len(rows)
        assert len(rows) == len(raw)  # no row was lost during deduplication

    def test_economic_calendar_uses_last_reported_not_reserved_word(self) -> None:
        rows = _rows(self._result("economic_calendar", "economic_calendar"), "calendar_economic")
        assert "last_reported" in rows[0]
        assert "last_value" not in rows[0]

    def test_ipo_calendar_handles_nat(self) -> None:
        """Filing/Amended Date was measured NaT in 3 of 3 rows."""
        rows = _rows(self._result("ipo_calendar", "ipo_calendar"), "calendar_ipo")
        assert rows
        assert all(r["filing_date"] is None or isinstance(r["filing_date"], date) for r in rows)

    def test_splits_calendar_computes_ratio(self) -> None:
        rows = _rows(self._result("splits_calendar", "splits_calendar"), "calendar_splits")
        assert rows
        row = next(r for r in rows if r["old_share_worth"])
        assert row["ratio"] == Decimal(row["share_worth"]) / Decimal(row["old_share_worth"])

    def test_empty_page_is_empty(self) -> None:
        result = MARKET_DATASETS["splits_calendar"].normalize(
            CalendarFramePayload(frame=None, fetched_at=FETCHED_AT)
        )
        assert result.is_empty


class TestMarketContext:
    def test_for_region_shares_cache(self) -> None:
        base = MarketContext(fetched_at=FETCHED_AT, start=date(2026, 9, 1), end=date(2026, 10, 1))
        calls: list[int] = []
        base.cached("k", lambda: calls.append(1))
        clone = base.for_region("US")
        clone.cached("k", lambda: calls.append(1))
        assert len(calls) == 1
        assert clone.region == "US"
        assert base.region is None


class _FakeWriter:
    def __init__(self, hashes: dict[tuple[Any, ...], str] | None = None) -> None:
        self.written: list[TableWrite] = []
        self.hashes = hashes or {}

    def write(self, write: TableWrite) -> int:
        self.written.append(write)
        return len(write.rows)

    def current_hash(self, table: str, key: Any) -> str | None:
        return self.hashes.get((table, *key.values()))

    def known_symbols(self, candidates: set[str]) -> set[str]:
        return set()


class _GatedDataset(HashGatedDataset[None]):
    name = "_test_gated"
    produces = ("financial_periods", "financial_facts")
    gate_table = "financial_periods"
    child_table = "financial_facts"
    gate_key_columns = GATE_KEY

    def fetch(self, ctx: Any) -> None:  # pragma: no cover - test base
        return None

    def normalize(self, raw: None, symbol: str) -> NormalizedResult:  # pragma: no cover
        return NormalizedResult()


def _gated_result(content_hash: str = "h1") -> NormalizedResult:
    key = {
        "symbol": "AAPL",
        "statement": "income",
        "freq": "annual",
        "period_end": date(2025, 9, 30),
    }
    period = {**key, "content_hash": content_hash, "fetched_at": FETCHED_AT, "item_count": 2}
    facts = [{**key, "item_key": "TotalRevenue", "value": Decimal("1")}]
    return NormalizedResult(
        writes=[
            TableWrite(
                table="financial_periods",
                rows=[period],
                key_columns=GATE_KEY,
                update_columns=("content_hash", "fetched_at", "item_count"),
            ),
            TableWrite(
                table="financial_facts",
                rows=facts,
                key_columns=(*GATE_KEY, "item_key"),
                update_columns=("value",),
                mode="replace_scope",
                scope_columns=GATE_KEY,
            ),
        ]
    )


class TestHashGate:
    def test_changed_hash_writes_facts_with_period_scope(self) -> None:
        writer = _FakeWriter()
        stats = _GatedDataset().upsert(writer, _gated_result())
        facts_write = next(w for w in writer.written if w.table == "financial_facts")
        assert facts_write.mode == "replace_scope"
        assert facts_write.scope_values is not None
        assert stats.attempted["financial_facts"] == 1

    def test_unchanged_hash_skips_facts_but_writes_period(self) -> None:
        """If the hash matches, child rows are not written; the header row
        is still written, and only fetched_at is updated."""
        key = ("financial_periods", "AAPL", "income", "annual", date(2025, 9, 30))
        writer = _FakeWriter({key: "h1"})
        stats = _GatedDataset().upsert(writer, _gated_result("h1"))

        period_writes = [w for w in writer.written if w.table == "financial_periods"]
        assert len(period_writes) == 1
        assert period_writes[0].update_columns == ("fetched_at",)
        assert stats.skipped["financial_facts"] == 1
        assert stats.attempted.get("financial_facts", 0) == 0
        assert not [w for w in writer.written if w.table == "financial_facts"]

    def test_full_refresh_writes_the_facts_even_when_the_hash_matches(self) -> None:
        """A period whose `financial_facts` were lost while its
        `financial_periods` header survived was unrepairable: the header
        hash still matched, the facts were reported `skipped`, and
        `--full-refresh` -- which only zeroed the watermark -- changed
        nothing about that."""
        key = ("financial_periods", "AAPL", "income", "annual", date(2025, 9, 30))
        writer = _FakeWriter({key: "h1"})
        stats = _GatedDataset().upsert(writer, _gated_result("h1"), full_refresh=True)

        facts_write = next(w for w in writer.written if w.table == "financial_facts")
        assert facts_write.mode == "replace_scope"
        assert stats.attempted["financial_facts"] == 1
        assert "financial_facts" not in stats.skipped
        # The header is rewritten in full, not touched on `fetched_at` only.
        period_write = next(w for w in writer.written if w.table == "financial_periods")
        assert period_write.update_columns == ("content_hash", "fetched_at", "item_count")

    def test_hash_read_before_any_write(self) -> None:
        order: list[str] = []

        class OrderingWriter(_FakeWriter):
            def current_hash(self, table: str, key: Any) -> str | None:
                order.append(f"read:{table}")
                return super().current_hash(table, key)

            def write(self, write: TableWrite) -> int:
                order.append(f"write:{write.table}")
                return super().write(write)

        _GatedDataset().upsert(OrderingWriter(), _gated_result())
        assert order[0] == "read:financial_periods"
        assert order.index("read:financial_periods") < order.index("write:financial_periods")


class TestSnapshotGate:
    """`snapshot_upsert` compares the history row against the snapshot row.

    The comparison is what `--full-refresh` has to be able to bypass: the
    snapshot row is exactly the one that survives when the _history rows
    are lost, so it keeps matching and the repair never writes anything.
    """

    @staticmethod
    def _result() -> NormalizedResult:
        row = {"region": "US", "content_hash": "h1", "fetched_at": FETCHED_AT}
        return NormalizedResult(
            writes=[
                TableWrite(
                    table="market_status",
                    rows=[row],
                    key_columns=("region",),
                    update_columns=("content_hash", "fetched_at"),
                ),
                TableWrite(
                    table="market_status_history",
                    rows=[row],
                    key_columns=("region", "fetched_at"),
                    update_columns=(),
                ),
            ]
        )

    def _upsert(self, writer: Any, **kwargs: Any) -> Any:
        return snapshot_upsert(
            writer,
            self._result(),
            snapshot_table="market_status",
            history_table="market_status_history",
            key_columns=("region",),
            **kwargs,
        )

    def test_matching_hash_skips_the_history_row(self) -> None:
        writer = _FakeWriter({("market_status", "US"): "h1"})
        stats = self._upsert(writer)
        assert stats.skipped["market_status_history"] == 1
        assert not [r for w in writer.written if w.table == "market_status_history" for r in w.rows]

    def test_full_refresh_keeps_the_history_row(self) -> None:
        writer = _FakeWriter({("market_status", "US"): "h1"})
        stats = self._upsert(writer, full_refresh=True)
        assert "market_status_history" not in stats.skipped
        assert stats.attempted["market_status_history"] == 1
