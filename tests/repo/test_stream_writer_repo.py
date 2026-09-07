"""The writer against a real PostgreSQL.

What can only be checked here: that the COPY path actually lands rows,
that the FK filter keeps one stray symbol from destroying a batch, and
that verification counts what is in the table rather than what the driver
claimed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from yfin.stream.protocol import REJECT_DECODE_FAILED, REJECT_UNKNOWN_SYMBOL, Reject
from yfin.stream.repository import StreamRepository
from yfin.stream.supervisor import StreamSupervisor, SupervisorConfig
from yfin.stream.writer import TICK_COLUMNS, StreamWriter, SymbolFilter, WriterConfig

pytestmark = pytest.mark.repo

TS = datetime(2026, 9, 7, 14, 30, tzinfo=UTC)


@pytest.fixture
def factory(db_session: Session) -> sessionmaker[Session]:
    """Sessions on the test's own connection, committing into a savepoint."""
    return sessionmaker(
        bind=db_session.connection(),
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )


@pytest.fixture
def writer(factory: sessionmaker[Session]) -> StreamWriter:
    repository = StreamRepository(factory)
    supervisor = StreamSupervisor(repository, config=SupervisorConfig())
    return StreamWriter(
        supervisor, repository, factory, config=WriterConfig(batch_interval_ms=10)
    )


def _seed(session: Session, *symbols: str) -> None:
    for symbol in symbols:
        session.execute(
            text(
                "INSERT INTO symbols (symbol, is_active, unknown_streak, created_at, updated_at) "
                "VALUES (:s, true, 0, now(), now()) ON CONFLICT (symbol) DO NOTHING"
            ),
            {"s": symbol},
        )
    session.commit()


def _row(symbol: str = "AAPL", ts: datetime = TS, payload_hash: str = "0" * 16) -> dict[str, Any]:
    row: dict[str, Any] = dict.fromkeys(TICK_COLUMNS)
    row.update(
        {
            "symbol": symbol,
            "ts_utc": ts,
            "payload_hash": payload_hash,
            "received_at": ts,
            "quote_type_code": 8,
            "market_hours_code": 1,
            "price": Decimal("232.35"),
            "day_volume": 1_234_567,
            "currency": "USD",
        }
    )
    return row


def _count(session: Session) -> int:
    return int(session.execute(text("SELECT count(*) FROM live_ticks")).scalar_one())


# --- the COPY path ---------------------------------------------------------


def test_copy_path_writes_rows(writer: StreamWriter, db_session: Session) -> None:
    _seed(db_session, "AAPL")
    with writer._session_factory() as session:
        written = writer._write_ticks(session, [_row()])
        session.commit()
    assert written == 1
    assert _count(db_session) == 1


def test_values_survive_the_round_trip(writer: StreamWriter, db_session: Session) -> None:
    """A float repr here would undo everything f32_decimal does."""
    _seed(db_session, "AAPL")
    with writer._session_factory() as session:
        writer._write_ticks(session, [_row()])
        session.commit()
    stored = db_session.execute(
        text("SELECT price, day_volume, currency, ts_utc FROM live_ticks")
    ).one()
    assert stored[0] == Decimal("232.35")
    assert stored[1] == 1_234_567
    assert stored[2] == "USD"
    assert stored[3] == TS


def test_nulls_survive_the_round_trip(writer: StreamWriter, db_session: Session) -> None:
    _seed(db_session, "AAPL")
    with writer._session_factory() as session:
        writer._write_ticks(session, [_row()])
        session.commit()
    assert db_session.execute(text("SELECT bid FROM live_ticks")).scalar_one() is None


def test_repeated_payload_is_deduped(writer: StreamWriter, db_session: Session) -> None:
    """Yahoo re-sends snapshots; ON CONFLICT DO NOTHING absorbs them."""
    _seed(db_session, "AAPL")
    for _ in range(2):
        with writer._session_factory() as session:
            writer._write_ticks(session, [_row()])
            session.commit()
    assert _count(db_session) == 1


def test_a_batch_writes_in_one_statement(writer: StreamWriter, db_session: Session) -> None:
    _seed(db_session, "AAPL", "MSFT")
    rows = [
        _row("AAPL", TS + timedelta(seconds=i), payload_hash=f"{i:016x}") for i in range(50)
    ] + [_row("MSFT", TS, payload_hash="f" * 16)]
    with writer._session_factory() as session:
        written = writer._write_ticks(session, rows)
        session.commit()
    assert written == 51
    assert _count(db_session) == 51


# --- the FK filter ---------------------------------------------------------


def test_one_unknown_symbol_does_not_lose_the_batch(
    writer: StreamWriter, db_session: Session
) -> None:
    """Without the filter the FK aborts the statement and 500 good ticks
    go with the one stray ticker."""
    _seed(db_session, "AAPL")
    rows = [_row("AAPL"), _row("NOPE", payload_hash="1" * 16)]
    with writer._session_factory() as session:
        written = writer._write_ticks(session, rows)
        session.commit()
    assert written == 1
    assert _count(db_session) == 1


def test_a_batch_of_only_unknown_symbols_writes_nothing(
    writer: StreamWriter, db_session: Session
) -> None:
    with writer._session_factory() as session:
        assert writer._write_ticks(session, [_row("NOPE")]) == 0


# --- verification ----------------------------------------------------------


def test_verification_counts_what_is_in_the_table(
    writer: StreamWriter, db_session: Session
) -> None:
    """Counts come from reading the keys back, not from the driver.

    A DO NOTHING conflict reports 0 affected rows, so a driver count would
    call a correct write a failure.
    """
    _seed(db_session, "AAPL")
    with writer._session_factory() as session:
        writer._write_ticks(session, [_row()])
        session.commit()
    # Same row again: nothing new is inserted, but the key is present.
    with writer._session_factory() as session:
        assert writer._write_ticks(session, [_row()]) == 1


# --- symbol filter ---------------------------------------------------------


def test_symbol_filter_finds_a_symbol_added_after_the_refresh(
    factory: sessionmaker[Session], db_session: Session
) -> None:
    """Miss-tolerant on purpose.

    Rejecting on a cache miss would discard every tick of a newly added
    symbol for a whole TTL window, and the reject sampling could throw
    away the evidence too.
    """
    _seed(db_session, "AAPL")
    symbol_filter = SymbolFilter(factory, ttl_seconds=3600)
    assert symbol_filter.known({"AAPL"}) == {"AAPL"}

    _seed(db_session, "MSFT")
    assert symbol_filter.known({"MSFT"}) == {"MSFT"}


def test_symbol_filter_still_rejects_what_does_not_exist(
    factory: sessionmaker[Session], db_session: Session
) -> None:
    _seed(db_session, "AAPL")
    symbol_filter = SymbolFilter(factory, ttl_seconds=3600)
    assert symbol_filter.known({"AAPL", "NOPE"}) == {"AAPL"}


def test_symbol_filter_handles_an_empty_request(factory: sessionmaker[Session]) -> None:
    assert SymbolFilter(factory, ttl_seconds=60).known(set()) == set()


# --- rejects ---------------------------------------------------------------


def test_rejects_are_written(writer: StreamWriter, db_session: Session) -> None:
    with writer._session_factory() as session:
        writer._write_rejects(
            session,
            [Reject(REJECT_UNKNOWN_SYMBOL, symbol="NOPE", detail="not in universe")],
        )
        session.commit()
    stored = db_session.execute(
        text("SELECT symbol, reason, detail FROM stream_rejects")
    ).one()
    assert stored[0] == "NOPE"
    assert stored[1] == REJECT_UNKNOWN_SYMBOL


def test_reject_keeps_the_wire_bytes(writer: StreamWriter, db_session: Session) -> None:
    """live_ticks has no raw_json; for an undecodable frame this is the
    only surviving evidence."""
    with writer._session_factory() as session:
        writer._write_rejects(
            session, [Reject(REJECT_DECODE_FAILED, raw_base64="Q0FGRQ==")]
        )
        session.commit()
    assert (
        db_session.execute(text("SELECT raw_base64 FROM stream_rejects")).scalar_one()
        == "Q0FGRQ=="
    )


def test_sampling_limits_what_reaches_the_table(
    factory: sessionmaker[Session], db_session: Session
) -> None:
    repository = StreamRepository(factory)
    supervisor = StreamSupervisor(repository, config=SupervisorConfig())
    writer = StreamWriter(
        supervisor, repository, factory, config=WriterConfig(reject_sample_per_hour=2)
    )
    with writer._session_factory() as session:
        writer._write_rejects(
            session, [Reject(REJECT_UNKNOWN_SYMBOL, symbol="NOPE") for _ in range(10)]
        )
        session.commit()
    assert (
        db_session.execute(text("SELECT count(*) FROM stream_rejects")).scalar_one() == 2
    )


# --- end to end ------------------------------------------------------------


def test_queue_to_table(writer: StreamWriter, db_session: Session) -> None:
    """The whole path: supervisor queue -> batch -> COPY -> table."""
    _seed(db_session, "AAPL")
    writer.set_session(
        StreamRepository(writer._session_factory).open_session(
            connection_count=1, symbol_count=1
        )
    )
    for i in range(5):
        writer._supervisor.queue.put(
            _row("AAPL", TS + timedelta(seconds=i), payload_hash=f"{i:016x}")
        )
    writer._cycle()
    assert _count(db_session) == 5
    assert writer.rows_written == 5


# --- live_quotes -----------------------------------------------------------


def _quote(session: Session) -> tuple[Any, ...] | None:
    row = session.execute(
        text("SELECT symbol, ts_utc, price FROM live_quotes")
    ).one_or_none()
    return tuple(row) if row is not None else None


def test_quotes_are_written_from_the_last_value_box(
    writer: StreamWriter, db_session: Session
) -> None:
    _seed(db_session, "AAPL")
    writer._supervisor.latest.put(_row("AAPL"))
    with writer._session_factory() as session:
        writer._write_quotes(session)
        session.commit()
    assert _quote(db_session) == ("AAPL", TS, Decimal("232.35"))


def test_a_newer_tick_moves_the_quote_forward(
    writer: StreamWriter, db_session: Session
) -> None:
    _seed(db_session, "AAPL")
    for offset in (0, 60):
        writer._supervisor.latest.put(_row("AAPL", TS + timedelta(seconds=offset)))
        with writer._session_factory() as session:
            writer._write_quotes(session)
            session.commit()
    stored = _quote(db_session)
    assert stored is not None
    assert stored[1] == TS + timedelta(seconds=60)


def test_an_older_tick_does_not_roll_the_quote_back(
    writer: StreamWriter, db_session: Session
) -> None:
    """The database half of the guard.

    Out-of-order delivery is normal on a reconnect: the server replays a
    snapshot. Without the guard the quote would jump backwards in time
    and disagree with the tick archive it summarises.
    """
    _seed(db_session, "AAPL")
    writer._supervisor.latest.put(_row("AAPL", TS))
    with writer._session_factory() as session:
        writer._write_quotes(session)
        session.commit()

    writer._supervisor.latest.put(_row("AAPL", TS - timedelta(minutes=5)))
    with writer._session_factory() as session:
        writer._write_quotes(session)
        session.commit()

    stored = _quote(db_session)
    assert stored is not None
    assert stored[1] == TS  # unchanged


def test_a_rejected_update_leaves_every_column_alone(
    writer: StreamWriter, db_session: Session
) -> None:
    """Not a per-column GREATEST: the whole row is refused or applied.

    Otherwise the stored quote would blend two instants -- one tick's
    price next to another's timestamp.
    """
    _seed(db_session, "AAPL")
    writer._supervisor.latest.put(_row("AAPL", TS))
    with writer._session_factory() as session:
        writer._write_quotes(session)
        session.commit()

    stale = _row("AAPL", TS - timedelta(minutes=5))
    stale["price"] = Decimal("999.99")
    writer._supervisor.latest.put(stale)
    with writer._session_factory() as session:
        writer._write_quotes(session)
        session.commit()

    stored = _quote(db_session)
    assert stored is not None
    assert stored[2] == Decimal("232.35")  # the stale price did not land


def test_draining_the_box_means_unchanged_symbols_are_not_rewritten(
    writer: StreamWriter, db_session: Session
) -> None:
    """The quotes upsert was 34% of the write path; skipping idle symbols
    is the cheapest saving available."""
    _seed(db_session, "AAPL")
    writer._supervisor.latest.put(_row("AAPL"))
    with writer._session_factory() as session:
        writer._write_quotes(session)
        session.commit()
    assert len(writer._supervisor.latest) == 0
    with writer._session_factory() as session:
        writer._write_quotes(session)  # nothing to do
        session.commit()
    assert _quote(db_session) is not None


def test_quotes_skip_unknown_symbols(writer: StreamWriter, db_session: Session) -> None:
    """live_quotes carries the same FK live_ticks does."""
    writer._supervisor.latest.put(_row("NOPE"))
    with writer._session_factory() as session:
        writer._write_quotes(session)
        session.commit()
    assert _quote(db_session) is None
