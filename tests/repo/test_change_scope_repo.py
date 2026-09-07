"""`replace_scope` as a diff, and bar writes as spans.

Both exist for the same reason: the naive shape publishes far more than
changed. Delete-and-reinsert would republish every row of ten dataset
modules' tables on every sync, and a first bar sync would emit one event per
bar to say "the history is here".
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from yfin.models import Symbol
from yfin.models.financials import FinancialFact, FinancialPeriod
from yfin.storage.changes import ChangeCollector, ChangeContext
from yfin.storage.contracts import TableWrite
from yfin.storage.persistence import PostgresRowWriter

pytestmark = pytest.mark.repo

PERIOD = date(2025, 9, 27)


@pytest.fixture
def symbol(db_session: Session) -> str:
    """A symbol, plus the `financial_periods` parent `financial_facts`
    carries an FK to."""
    db_session.add(Symbol(symbol="AAPL", is_active=True))
    db_session.flush()
    db_session.add(
        FinancialPeriod(
            symbol="AAPL",
            statement="income",
            freq="annual",
            period_end=PERIOD,
            item_count=0,
            raw_json="{}",
            content_hash="0" * 64,
            fetched_at=datetime(2026, 9, 7, tzinfo=UTC),
        )
    )
    db_session.flush()
    return "AAPL"


def _collector(threshold: int = 1000) -> ChangeCollector:
    return ChangeCollector(ChangeContext(run_id=3, range_threshold=threshold))


def _events(collector: ChangeCollector) -> list[dict[str, Any]]:
    return [json.loads(e.payload) for e in collector.pending]


def _facts(**items: object) -> TableWrite:
    """A `replace_scope` write over one (symbol, statement, freq, period)."""
    return TableWrite(
        table="financial_facts",
        rows=[
            {
                "symbol": "AAPL",
                "statement": "income",
                "freq": "annual",
                "period_end": PERIOD,
                "item_key": key,
                "value": value,
            }
            for key, value in items.items()
        ],
        key_columns=("symbol", "statement", "freq", "period_end", "item_key"),
        update_columns=("value",),
        mode="replace_scope",
        scope_columns=("symbol", "statement", "freq", "period_end"),
    )


class TestReplaceScopeDiff:
    def test_a_removed_key_becomes_a_delete(self, db_session: Session, symbol: str) -> None:
        PostgresRowWriter(db_session).write(_facts(Revenue=1, CostOfRevenue=2))
        collector = _collector()
        PostgresRowWriter(db_session, collector=collector).write(_facts(Revenue=1))
        events = _events(collector)
        assert [(e["op"], e["key"]["item_key"]) for e in events] == [
            ("delete", "CostOfRevenue")
        ]

    def test_an_unchanged_key_yields_nothing(self, db_session: Session, symbol: str) -> None:
        """The whole reason the diff exists: ten dataset modules use
        `replace_scope`, and delete-plus-insert would republish all of it
        every sync."""
        PostgresRowWriter(db_session).write(_facts(Revenue=1, CostOfRevenue=2))
        collector = _collector()
        PostgresRowWriter(db_session, collector=collector).write(
            _facts(Revenue=1, CostOfRevenue=2)
        )
        assert _events(collector) == []

    def test_a_new_key_is_an_insert_and_a_changed_one_an_update(
        self, db_session: Session, symbol: str
    ) -> None:
        PostgresRowWriter(db_session).write(_facts(Revenue=1))
        collector = _collector()
        PostgresRowWriter(db_session, collector=collector).write(
            _facts(Revenue=9, CostOfRevenue=2)
        )
        by_key = {e["key"]["item_key"]: e["op"] for e in _events(collector)}
        assert by_key == {"Revenue": "update", "CostOfRevenue": "insert"}

    def test_the_delete_actually_happens(self, db_session: Session, symbol: str) -> None:
        PostgresRowWriter(db_session).write(_facts(Revenue=1, CostOfRevenue=2))
        PostgresRowWriter(db_session, collector=_collector()).write(_facts(Revenue=1))
        stored = db_session.execute(select(FinancialFact.item_key)).scalars().all()
        assert stored == ["Revenue"]

    def test_an_empty_write_clears_the_whole_scope(
        self, db_session: Session, symbol: str
    ) -> None:
        """With no incoming rows nothing is in `incoming`, so everything in
        scope is removed -- which is what today's single DELETE does."""
        PostgresRowWriter(db_session).write(_facts(Revenue=1, CostOfRevenue=2))
        empty = TableWrite(
            table="financial_facts",
            rows=[],
            key_columns=("symbol", "statement", "freq", "period_end", "item_key"),
            update_columns=("value",),
            mode="replace_scope",
            scope_columns=("symbol", "statement", "freq", "period_end"),
            scope_values=(
                {
                    "symbol": "AAPL",
                    "statement": "income",
                    "freq": "annual",
                    "period_end": PERIOD,
                },
            ),
        )
        collector = _collector()
        PostgresRowWriter(db_session, collector=collector).write(empty)
        assert {e["op"] for e in _events(collector)} == {"delete"}
        assert db_session.execute(select(FinancialFact.item_key)).scalars().all() == []

    def test_without_a_collector_the_scope_is_still_cleared(
        self, db_session: Session, symbol: str
    ) -> None:
        """The single DELETE stays exactly as it was."""
        PostgresRowWriter(db_session).write(_facts(Revenue=1, CostOfRevenue=2))
        PostgresRowWriter(db_session).write(_facts(Revenue=1))
        stored = db_session.execute(select(FinancialFact.item_key)).scalars().all()
        assert stored == ["Revenue"]

    def test_the_update_map_widens_to_every_non_key_column(
        self, db_session: Session, symbol: str
    ) -> None:
        """`financial_facts` declares `update_columns=("value",)`, which was
        fine when the row had just been deleted and reinserted. On the diff
        path a partial update map would leave every other column at the old
        row's value, so the map widens -- and this is the check that the two
        paths still agree."""
        PostgresRowWriter(db_session).write(_facts(Revenue=1))
        PostgresRowWriter(db_session, collector=_collector()).write(_facts(Revenue=7))
        value = db_session.execute(select(FinancialFact.value)).scalar_one()
        assert value == 7


class TestRangeCoalescing:
    """`price_bars` is the reason: ~20,000 rows per symbol on a first sync."""

    def _bars(self, count: int, interval: str = "1m") -> TableWrite:
        base = datetime(2026, 9, 7, 9, 30, tzinfo=UTC)
        return TableWrite(
            table="price_bars",
            rows=[
                {
                    "symbol": "AAPL",
                    "bar_interval": interval,
                    "ts_utc": base.replace(minute=30 + i % 25, second=i // 25),
                    "local_date": base.date(),
                    "close": 1,
                    "is_extended": False,
                }
                for i in range(count)
            ],
            key_columns=("symbol", "bar_interval", "ts_utc"),
            update_columns=("close",),
        )

    def test_above_the_threshold_the_rows_become_one_span(
        self, db_session: Session, symbol: str
    ) -> None:
        write = self._bars(12)
        collector = _collector(threshold=5)
        PostgresRowWriter(db_session, collector=collector).write(write)
        (event,) = _events(collector)
        assert event["op"] == "range"
        assert event["key"] == {"symbol": "AAPL"}
        assert event["row"]["kind"] == "write"
        assert event["row"]["bar_interval"] == "1m"
        assert event["row"]["ts_column"] == "ts_utc"
        assert event["row"]["rows"] == 12

    def test_below_the_threshold_the_rows_stay_rows(
        self, db_session: Session, symbol: str
    ) -> None:
        """Steady-state daily writes are ~390 one-minute bars, and a
        consumer can apply those directly."""
        collector = _collector(threshold=100)
        PostgresRowWriter(db_session, collector=collector).write(self._bars(12))
        assert {e["op"] for e in _events(collector)} == {"insert"}

    def test_the_span_covers_the_rows_written(
        self, db_session: Session, symbol: str
    ) -> None:
        write = self._bars(12)
        stamps = sorted(row["ts_utc"] for row in write.rows)
        collector = _collector(threshold=5)
        PostgresRowWriter(db_session, collector=collector).write(write)
        row = _events(collector)[0]["row"]
        assert row["ts_from"] == stamps[0].isoformat()
        assert row["ts_to"] == stamps[-1].isoformat()

    def test_one_span_per_interval(self, db_session: Session, symbol: str) -> None:
        """Two intervals in one write are two different series; one span
        over both would name a range neither of them has."""
        rows = [*self._bars(8, "1m").rows, *self._bars(8, "5m").rows]
        write = TableWrite(
            table="price_bars",
            rows=rows,
            key_columns=("symbol", "bar_interval", "ts_utc"),
            update_columns=("close",),
        )
        collector = _collector(threshold=5)
        PostgresRowWriter(db_session, collector=collector).write(write)
        events = _events(collector)
        assert {e["row"]["bar_interval"] for e in events} == {"1m", "5m"}
        assert all(e["op"] == "range" for e in events)

    def test_a_repair_stays_row_level(self, db_session: Session, symbol: str) -> None:
        """Updates are repairs, and a repair is small and worth applying
        directly however many inserts share the statement."""
        write = self._bars(12)
        PostgresRowWriter(db_session).write(write)
        changed = TableWrite(
            table="price_bars",
            rows=[{**row, "close": 99} for row in write.rows],
            key_columns=("symbol", "bar_interval", "ts_utc"),
            update_columns=("close",),
        )
        collector = _collector(threshold=5)
        PostgresRowWriter(db_session, collector=collector).write(changed)
        assert {e["op"] for e in _events(collector)} == {"update"}

    def test_a_non_bars_table_never_coalesces(
        self, db_session: Session, symbol: str
    ) -> None:
        """Coalescing hides the row, and outside the bars family the row is
        what the consumer wanted."""
        collector = _collector(threshold=1)
        PostgresRowWriter(db_session, collector=collector).write(
            _facts(Revenue=1, CostOfRevenue=2, GrossProfit=3)
        )
        assert {e["op"] for e in _events(collector)} == {"insert"}
