"""Deletes that happen outside a sync, and what they publish.

`symbols purge` and `yfin prune` remove rows nothing else will ever mention
again. A consumer mirroring the archive has to be told, or its copy keeps
rows that exist nowhere -- which is the same failure the whole design exists
to prevent, arriving from the other direction.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from yfin.models import Symbol
from yfin.models.bars import PriceBar
from yfin.models.prices import Dividend
from yfin.pipeline.prune import prune_calendars
from yfin.storage.changes import ChangeCollector, ChangeContext
from yfin.storage.purge import delete_rows, purge_symbol

pytestmark = pytest.mark.repo


@pytest.fixture
def symbol(db_session: Session) -> str:
    db_session.add(Symbol(symbol="AAPL", is_active=True))
    db_session.flush()
    return "AAPL"


def _collector() -> ChangeCollector:
    """Outside a sync, so `run_id` is null -- there is no run to name."""
    return ChangeCollector(ChangeContext(run_id=None, range_threshold=1000))


def _events(collector: ChangeCollector) -> list[dict[str, Any]]:
    return [json.loads(e.payload) for e in collector.pending]


def _bar(minute: int, interval: str = "1m") -> PriceBar:
    return PriceBar(
        symbol="AAPL",
        bar_interval=interval,
        ts_utc=datetime(2026, 9, 7, 14, minute, tzinfo=UTC),
        local_date=date(2026, 9, 7),
        close=1,
        is_extended=False,
    )


class TestPurge:
    def test_a_row_level_table_yields_one_delete_per_row(
        self, db_session: Session, symbol: str
    ) -> None:
        db_session.add(Dividend(symbol="AAPL", ex_date=date(2026, 1, 1), amount=1))
        db_session.add(Dividend(symbol="AAPL", ex_date=date(2026, 4, 1), amount=1))
        db_session.flush()

        collector = _collector()
        purge_symbol(db_session, "AAPL", collector=collector)
        dividends = [e for e in _events(collector) if e["table"] == "dividends"]
        # `dividends` is a bars-family table, so it coalesces -- see below.
        assert [e["op"] for e in dividends] == ["range"]

    def test_the_symbol_row_itself_is_a_delete(
        self, db_session: Session, symbol: str
    ) -> None:
        collector = _collector()
        purge_symbol(db_session, "AAPL", collector=collector)
        symbols = [e for e in _events(collector) if e["table"] == "symbols"]
        assert [(e["op"], e["key"]) for e in symbols] == [
            ("delete", {"symbol": "AAPL"})
        ]

    def test_a_purge_names_no_dataset_and_no_run(
        self, db_session: Session, symbol: str
    ) -> None:
        """It runs outside both, and inventing either would put a value in
        the envelope that resolves to nothing."""
        collector = _collector()
        purge_symbol(db_session, "AAPL", collector=collector)
        for event in _events(collector):
            assert event["dataset"] is None
            assert event["run_id"] is None

    def test_bars_are_one_span_per_interval_not_one_event_per_bar(
        self, db_session: Session, symbol: str
    ) -> None:
        """Returning millions of bar keys from a DELETE is the cost the
        range event exists to avoid."""
        for minute in range(5):
            db_session.add(_bar(minute, "1m"))
        for minute in range(3):
            db_session.add(_bar(minute, "5m"))
        db_session.flush()

        collector = _collector()
        purge_symbol(db_session, "AAPL", collector=collector)
        bars = [e for e in _events(collector) if e["table"] == "price_bars"]
        assert {e["op"] for e in bars} == {"range"}
        by_interval = {e["row"]["bar_interval"]: e["row"] for e in bars}
        assert by_interval["1m"]["kind"] == "delete"
        assert by_interval["1m"]["rows"] == 5
        assert by_interval["5m"]["rows"] == 3

    def test_the_purge_span_is_left_open(self, db_session: Session, symbol: str) -> None:
        """Reading min and max back would scan the very table whose size is
        the reason this event exists, and a consumer clearing a symbol does
        not need a range to do it."""
        db_session.add(_bar(0))
        db_session.flush()
        collector = _collector()
        purge_symbol(db_session, "AAPL", collector=collector)
        (bars,) = [e for e in _events(collector) if e["table"] == "price_bars"]
        assert (bars["row"]["ts_from"], bars["row"]["ts_to"]) == (None, None)

    def test_the_rows_are_actually_gone(self, db_session: Session, symbol: str) -> None:
        db_session.add(_bar(0))
        db_session.add(Dividend(symbol="AAPL", ex_date=date(2026, 1, 1), amount=1))
        db_session.flush()
        purge_symbol(db_session, "AAPL", collector=_collector())
        assert db_session.execute(select(PriceBar.symbol)).scalars().all() == []
        assert db_session.execute(select(Symbol.symbol)).scalars().all() == []

    def test_without_a_collector_it_still_purges(
        self, db_session: Session, symbol: str
    ) -> None:
        db_session.add(_bar(0))
        db_session.flush()
        removed = purge_symbol(db_session, "AAPL")
        assert removed["price_bars"] == 1
        assert db_session.execute(select(Symbol.symbol)).scalars().all() == []


class TestPrune:
    def test_a_pruned_calendar_row_is_published(self, db_session: Session) -> None:
        db_session.execute(
            text(
                "INSERT INTO calendar_economic "
                "  (region, event_time_utc, event_name, fetched_at) "
                "VALUES ('US', :old, 'CPI', :now)"
            ),
            {"old": datetime(2020, 1, 1, tzinfo=UTC), "now": datetime(2026, 9, 7, tzinfo=UTC)},
        )
        collector = _collector()
        removed = prune_calendars(
            db_session, datetime(2026, 1, 1, tzinfo=UTC), collector=collector
        )
        assert removed["calendar_economic"] == 1
        (event,) = [e for e in _events(collector) if e["table"] == "calendar_economic"]
        assert event["op"] == "delete"
        assert event["key"] == {
            "region": "US",
            "event_time_utc": "2020-01-01T00:00:00+00:00",
            "event_name": "CPI",
        }

    def test_a_dry_run_publishes_nothing(self, db_session: Session) -> None:
        """It counts; it must not claim rows went that are still there."""
        db_session.execute(
            text(
                "INSERT INTO calendar_economic "
                "  (region, event_time_utc, event_name, fetched_at) "
                "VALUES ('US', :old, 'CPI', :now)"
            ),
            {"old": datetime(2020, 1, 1, tzinfo=UTC), "now": datetime(2026, 9, 7, tzinfo=UTC)},
        )
        collector = _collector()
        prune_calendars(
            db_session, datetime(2026, 1, 1, tzinfo=UTC), dry_run=True, collector=collector
        )
        assert collector.pending == []


def test_a_bars_table_is_refused_by_the_row_level_delete(
    db_session: Session, symbol: str
) -> None:
    """The helper would return one key per bar, which is exactly what the
    range event exists to avoid; `purge_symbol` routes them elsewhere."""
    from yfin.models.base import Base

    with pytest.raises(ValueError, match="range events"):
        delete_rows(
            db_session,
            Base.metadata.tables["price_bars"],
            Base.metadata.tables["price_bars"].c["symbol"] == "AAPL",
            collector=_collector(),
        )
