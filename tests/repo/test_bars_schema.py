"""price_bars schema behavior: hypertable, chunk, view.

Hypertables are not created by `create_all()`; conftest applies the migration's
`timescale_ddl()` fixture, and these tests prove it partitions and chunks.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from yfin.models import PriceBar

pytestmark = pytest.mark.repo


def _bar(ts: datetime, *, symbol: str = "AAPL", interval: str = "5m") -> PriceBar:
    return PriceBar(
        symbol=symbol,
        bar_interval=interval,
        ts_utc=ts,
        local_date=ts.date(),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=1000,
        is_extended=False,
    )


def _seed_symbol(session: Session, symbol: str = "AAPL") -> None:
    """price_bars carries an FK to symbols, so the parent row must exist first."""
    session.execute(
        text(
            "INSERT INTO symbols (symbol, is_active, unknown_streak, created_at, updated_at) "
            "VALUES (:s, true, 0, now(), now()) ON CONFLICT (symbol) DO NOTHING"
        ),
        {"s": symbol},
    )


def test_price_bars_is_a_hypertable(db_session: Session) -> None:
    """Partitioning column and chunk interval must match what's claimed."""
    row = db_session.execute(
        text(
            # `hypertable_schema` filter is required: the view does not filter
            # by schema, and the same name exists in both `public` (production)
            # and the process-specific test schema -> MultipleResultsFound.
            "SELECT column_name, column_type, time_interval "
            "  FROM timescaledb_information.dimensions "
            " WHERE hypertable_schema = current_schema() "
            "   AND hypertable_name = 'price_bars'"
        )
    ).one()

    assert row.column_name == "ts_utc"
    assert "time zone" in row.column_type, "partitioning column must be timestamptz"
    assert row.time_interval == timedelta(days=7)


def test_price_history_is_a_hypertable(db_session: Session) -> None:
    row = db_session.execute(
        text(
            "SELECT column_name, time_interval "
            "  FROM timescaledb_information.dimensions "
            " WHERE hypertable_schema = current_schema() "
            "   AND hypertable_name = 'price_history'"
        )
    ).one()

    assert row.column_name == "session_date"
    assert row.time_interval == timedelta(days=365)


def test_no_default_partition_index_is_created(db_session: Session) -> None:
    """Without `create_default_indexes => FALSE`, Alembic's "empty diff" check
    never passes: the default DESC index sits in `public`, is absent from
    Base.metadata, and autogenerate reports it as "should be dropped"."""
    names = set(
        db_session.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                " WHERE tablename IN ('price_bars', 'price_history')"
            )
        )
        .scalars()
        .all()
    )

    assert "price_bars_ts_utc_idx" not in names
    assert "price_history_session_date_idx" not in names


def test_chunks_are_created_on_write(committed_session: Session, cleanup_tables: list[str]) -> None:
    """TimescaleDB opens a chunk at write time on its own; there is no
    "out of range" case for a far-future bar."""
    cleanup_tables.extend(["price_bars", "symbols"])
    _seed_symbol(committed_session)
    committed_session.add(_bar(datetime(2026, 9, 2, 14, 30, tzinfo=UTC)))
    # Far enough apart to land in two different chunks, and a future date
    committed_session.add(_bar(datetime(2029, 1, 2, 14, 30, tzinfo=UTC)))
    committed_session.commit()

    chunks = committed_session.execute(
        text(
            "SELECT count(*) FROM timescaledb_information.chunks "
            " WHERE hypertable_schema = current_schema() "
            "   AND hypertable_name = 'price_bars'"
        )
    ).scalar_one()

    assert chunks >= 2, "chunks should be created at write time"


def test_range_query_descends_to_chunks(db_session: Session) -> None:
    """The dominant query pattern must descend to chunk level (chunk exclusion)."""
    plan = "\n".join(
        db_session.execute(
            text(
                "EXPLAIN SELECT close FROM price_bars "
                "WHERE symbol = 'AAPL' AND bar_interval = '5m' "
                "AND ts_utc >= '2026-09-01' AND ts_utc < '2026-10-01'"
            )
        )
        .scalars()
        .all()
    )

    assert "_hyper_" in plan or "Chunk" in plan, f"did not descend to a chunk:\n{plan}"


def test_view_hides_extended_session_bars(db_session: Session) -> None:
    """v_price_bars_regular hides bars outside the regular session."""
    _seed_symbol(db_session)
    regular = _bar(datetime(2026, 9, 2, 14, 30, tzinfo=UTC))
    extended = _bar(datetime(2026, 9, 2, 8, 5, tzinfo=UTC))
    extended.is_extended = True
    db_session.add_all([regular, extended])
    db_session.flush()

    seen = (
        db_session.execute(
            text(
                "SELECT ts_utc FROM v_price_bars_regular "
                "WHERE symbol = 'AAPL' AND bar_interval = '5m'"
            )
        )
        .scalars()
        .all()
    )

    assert seen == [datetime(2026, 9, 2, 14, 30, tzinfo=UTC)]


def test_orphan_bars_are_impossible(db_session: Session) -> None:
    """price_bars carries an FK to symbols, so an orphan bar cannot be written."""
    from sqlalchemy.exc import IntegrityError

    db_session.add(_bar(datetime(2026, 9, 2, 14, 30, tzinfo=UTC), symbol="ZZNOSYMBOL"))
    with pytest.raises(IntegrityError) as excinfo:
        db_session.flush()

    assert "fk_price_bars_symbol_symbols" in str(excinfo.value)
