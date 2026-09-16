"""The collecting writer against a real database.

The unit tests read the statement; these run it: which rows PostgreSQL hands
back, and whether volatile columns still land where a plain upsert puts them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from yfin.models import Symbol
from yfin.models.analysis import AnalystPriceTarget
from yfin.storage.changes import ChangeCollector, ChangeContext
from yfin.storage.contracts import TableWrite
from yfin.storage.persistence import PostgresRowWriter

pytestmark = pytest.mark.repo

DAY = "2026-09-07"


@pytest.fixture
def symbol(db_session: Session) -> str:
    db_session.add(Symbol(symbol="AAPL", is_active=True))
    db_session.flush()
    return "AAPL"


def _collector() -> ChangeCollector:
    return ChangeCollector(ChangeContext(run_id=7, range_threshold=1000))


def _write(*, current: object, fetched_at: datetime, **overrides: object) -> TableWrite:
    defaults: dict[str, object] = {
        "table": "analyst_price_targets",
        "rows": [
            {
                "symbol": "AAPL",
                "as_of_date": DAY,
                "current": current,
                "fetched_at": fetched_at,
            }
        ],
        "key_columns": ("symbol", "as_of_date"),
        "update_columns": ("current", "fetched_at"),
    }
    defaults.update(overrides)
    return TableWrite(**defaults)  # type: ignore[arg-type]


def _ops(collector: ChangeCollector) -> list[str]:
    return [str(json.loads(e.payload)["op"]) for e in collector.pending]


def _stored(session: Session) -> tuple[Decimal | None, datetime]:
    return session.execute(
        select(AnalystPriceTarget.current, AnalystPriceTarget.fetched_at)
    ).one()


T0 = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)
T1 = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
T2 = datetime(2026, 9, 7, 11, 0, tzinfo=UTC)


def test_a_first_write_is_one_insert(db_session: Session, symbol: str) -> None:
    collector = _collector()
    PostgresRowWriter(db_session, collector=collector).write(_write(current=1, fetched_at=T0))
    assert _ops(collector) == ["insert"]


def test_the_same_rows_written_twice_yield_events_once(
    db_session: Session, symbol: str
) -> None:
    """The predicate is what stops a daily sync from republishing the whole
    archive every morning."""
    writer_one = _collector()
    PostgresRowWriter(db_session, collector=writer_one).write(_write(current=1, fetched_at=T0))
    writer_two = _collector()
    PostgresRowWriter(db_session, collector=writer_two).write(_write(current=1, fetched_at=T1))
    assert _ops(writer_one) == ["insert"]
    assert _ops(writer_two) == []


def test_a_changed_value_yields_one_update(db_session: Session, symbol: str) -> None:
    PostgresRowWriter(db_session).write(_write(current=1, fetched_at=T0))
    collector = _collector()
    PostgresRowWriter(db_session, collector=collector).write(_write(current=2, fetched_at=T1))
    assert _ops(collector) == ["update"]


def test_the_event_carries_the_row_the_database_has(
    db_session: Session, symbol: str
) -> None:
    """`RETURNING *`, so columns outside the update map and server defaults
    are what the consumer sees -- not what the pipeline proposed."""
    collector = _collector()
    PostgresRowWriter(db_session, collector=collector).write(_write(current=1, fetched_at=T0))
    envelope = json.loads(collector.pending[0].payload)
    assert envelope["row"]["symbol"] == "AAPL"
    assert envelope["row"]["current"] == "1.000000000000"
    assert envelope["key"] == {"symbol": "AAPL", "as_of_date": DAY}


class TestVolatileColumns:
    """A row whose only difference is `fetched_at` did not change -- but the
    column still has to move, or `HashGate` reads a frozen date."""

    def test_an_unchanged_row_still_gets_its_fetched_at(
        self, db_session: Session, symbol: str
    ) -> None:
        PostgresRowWriter(db_session).write(_write(current=1, fetched_at=T0))
        collector = _collector()
        PostgresRowWriter(db_session, collector=collector).write(
            _write(current=1, fetched_at=T1)
        )
        assert _ops(collector) == []
        current, fetched_at = _stored(db_session)
        assert fetched_at == T1
        assert current == Decimal("1")

    def test_each_row_gets_its_own_proposed_value(
        self, db_session: Session, symbol: str
    ) -> None:
        """The touch is `FROM (VALUES ...)`, not one UPDATE per value, so two
        unchanged rows with different timestamps do not collapse."""
        db_session.add(Symbol(symbol="MSFT", is_active=True))
        db_session.flush()
        rows = [
            {"symbol": "AAPL", "as_of_date": DAY, "current": 1, "fetched_at": T0},
            {"symbol": "MSFT", "as_of_date": DAY, "current": 5, "fetched_at": T0},
        ]
        base = TableWrite(
            table="analyst_price_targets",
            rows=rows,
            key_columns=("symbol", "as_of_date"),
            update_columns=("current", "fetched_at"),
        )
        PostgresRowWriter(db_session).write(base)

        again = TableWrite(
            table="analyst_price_targets",
            rows=[
                {"symbol": "AAPL", "as_of_date": DAY, "current": 1, "fetched_at": T1},
                {"symbol": "MSFT", "as_of_date": DAY, "current": 5, "fetched_at": T2},
            ],
            key_columns=("symbol", "as_of_date"),
            update_columns=("current", "fetched_at"),
        )
        collector = _collector()
        PostgresRowWriter(db_session, collector=collector).write(again)
        assert _ops(collector) == []
        stored = dict(
            db_session.execute(
                select(AnalystPriceTarget.symbol, AnalystPriceTarget.fetched_at)
            ).all()
        )
        assert stored == {"AAPL": T1, "MSFT": T2}

    def test_a_changed_row_is_not_touched_twice(
        self, db_session: Session, symbol: str
    ) -> None:
        """It was updated by the upsert, volatile columns included; the touch
        is only for the rows the RETURNING did not report."""
        PostgresRowWriter(db_session).write(_write(current=1, fetched_at=T0))
        collector = _collector()
        PostgresRowWriter(db_session, collector=collector).write(
            _write(current=9, fetched_at=T1)
        )
        assert _ops(collector) == ["update"]
        current, fetched_at = _stored(db_session)
        assert (current, fetched_at) == (Decimal("9"), T1)

    def test_the_touch_emits_nothing(self, db_session: Session, symbol: str) -> None:
        PostgresRowWriter(db_session).write(_write(current=1, fetched_at=T0))
        collector = _collector()
        PostgresRowWriter(db_session, collector=collector).write(
            _write(current=1, fetched_at=T1)
        )
        assert collector.pending == []


class TestWithoutACollector:
    """The path every call site takes until the runners are wired up."""

    def test_an_unchanged_row_is_written_as_before(
        self, db_session: Session, symbol: str
    ) -> None:
        """No predicate, so the upsert writes unconditionally -- which is
        what the volatile touch reproduces on the collecting path."""
        PostgresRowWriter(db_session).write(_write(current=1, fetched_at=T0))
        PostgresRowWriter(db_session).write(_write(current=1, fetched_at=T1))
        current, fetched_at = _stored(db_session)
        assert (current, fetched_at) == (Decimal("1"), T1)

    def test_verification_still_counts_every_key(
        self, db_session: Session, symbol: str
    ) -> None:
        """`_verify` is untouched: the events are a by-product, not the
        proof."""
        written = PostgresRowWriter(db_session, collector=_collector()).write(
            _write(current=1, fetched_at=T0)
        )
        assert written == 1
        again = PostgresRowWriter(db_session, collector=_collector()).write(
            _write(current=1, fetched_at=T1)
        )
        assert again == 1


class TestAllVolatileWrite:
    """An all-volatile update map gets no predicate and no RETURNING clause (the
    hash gate's `fetched_at`-only write on a data table). Reading that result
    raises `ResourceClosedError` and takes the whole symbol transaction with it;
    only execution, not compilation, can show it."""

    def _write(self) -> TableWrite:
        return TableWrite(
            table="analyst_price_targets",
            rows=[{"symbol": "AAPL", "as_of_date": DAY, "fetched_at": T1}],
            key_columns=("symbol", "as_of_date"),
            update_columns=("fetched_at",),
        )

    def test_it_does_not_raise(self, db_session: Session, symbol: str) -> None:
        PostgresRowWriter(db_session).write(_write(current=1, fetched_at=T0))
        PostgresRowWriter(db_session, collector=_collector()).write(self._write())

    def test_it_emits_nothing(self, db_session: Session, symbol: str) -> None:
        """'We checked' is not a change anyone can apply."""
        PostgresRowWriter(db_session).write(_write(current=1, fetched_at=T0))
        collector = _collector()
        PostgresRowWriter(db_session, collector=collector).write(self._write())
        assert collector.pending == []

    def test_it_still_writes_the_column(self, db_session: Session, symbol: str) -> None:
        """`HashGate` reads `fetched_at` as 'last verified at'; freezing it
        would make every run look like the first."""
        PostgresRowWriter(db_session).write(_write(current=1, fetched_at=T0))
        PostgresRowWriter(db_session, collector=_collector()).write(self._write())
        _current, fetched_at = _stored(db_session)
        assert fetched_at == T1


def test_an_infrastructure_table_emits_nothing(db_session: Session, symbol: str) -> None:
    """`asof_state` says 'we checked', not 'this moved'."""
    collector = _collector()
    PostgresRowWriter(db_session, collector=collector).write(
        TableWrite(
            table="asof_state",
            rows=[
                {
                    "symbol": "AAPL",
                    "dataset": "analyst_price_targets",
                    "as_of_date": DAY,
                    "content_hash": "abc",
                    "row_count": 1,
                    "fetched_at": T0,
                    "first_seen_at": T0,
                }
            ],
            key_columns=("symbol", "dataset"),
            update_columns=("as_of_date", "content_hash", "row_count", "fetched_at"),
        )
    )
    assert collector.pending == []
    stored = db_session.execute(text("SELECT count(*) FROM asof_state")).scalar_one()
    assert stored == 1
