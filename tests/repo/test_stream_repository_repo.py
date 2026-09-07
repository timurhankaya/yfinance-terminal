"""The stream package's SQL, against a real database.

The supervisor tests fake this layer; these check the queries themselves.
Two of them matter more than the rest: the scope join has to exclude
deactivated symbols, and the health table has to make a crashed process
distinguishable from a running one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from yfin.models.stream import StreamStatus
from yfin.stream.repository import StreamRepository

pytestmark = pytest.mark.repo

TS = datetime(2026, 9, 7, 14, 30, tzinfo=UTC)


@pytest.fixture
def repository(db_session: Session) -> StreamRepository:
    """Bound to the test's own connection, not a fresh one.

    `db_session` runs each test inside a transaction it rolls back at the
    end. A repository with its own engine would open a second connection,
    see none of the test's rows, and commit its own for real -- so it
    joins the outer transaction and its commits become savepoints.
    """
    return StreamRepository(
        sessionmaker(
            bind=db_session.connection(),
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
    )


def _symbol(
    session: Session, symbol: str, *, exchange: str | None = "NMS", active: bool = True
) -> None:
    session.execute(
        text(
            "INSERT INTO symbols (symbol, exchange, is_active, unknown_streak, "
            "                     created_at, updated_at) "
            "VALUES (:s, :e, :a, 0, now(), now()) "
            "ON CONFLICT (symbol) DO UPDATE SET exchange = :e, is_active = :a"
        ),
        {"s": symbol, "e": exchange, "a": active},
    )


def _scope(session: Session, symbol: str, *, enabled: bool = True, archive: bool = True) -> None:
    session.execute(
        text(
            "INSERT INTO stream_scope (symbol, enabled, archive, added_at) "
            "VALUES (:s, :en, :ar, :ts) ON CONFLICT (symbol) DO UPDATE "
            "SET enabled = :en, archive = :ar"
        ),
        {"s": symbol, "en": enabled, "ar": archive, "ts": TS},
    )


@pytest.fixture
def seeded(db_session: Session) -> Session:
    for symbol, exchange, active in [
        ("AAPL", "NMS", True),
        ("MSFT", "NMS", True),
        ("XU100.IS", "IST", True),
        ("DEAD", "NMS", False),
        ("NOEXCH", None, True),
    ]:
        _symbol(db_session, symbol, exchange=exchange, active=active)
    for symbol in ("AAPL", "MSFT", "XU100.IS", "DEAD", "NOEXCH"):
        _scope(db_session, symbol)
    _scope(db_session, "MSFT", archive=False)
    db_session.commit()
    return db_session


# --- scope -----------------------------------------------------------------


def test_scope_excludes_deactivated_symbols(
    repository: StreamRepository, seeded: Session
) -> None:
    """The condition that stops a delisted symbol streaming forever.

    `known_symbols()` only checks that the row exists, not that it is
    active, so nothing further down the write path would catch this.
    """
    symbols = {entry.symbol for entry in repository.load_scope()}
    assert "DEAD" not in symbols
    assert {"AAPL", "MSFT", "XU100.IS", "NOEXCH"} <= symbols


def test_scope_excludes_disabled_rows(
    repository: StreamRepository, seeded: Session
) -> None:
    _scope(seeded, "AAPL", enabled=False)
    seeded.commit()
    assert "AAPL" not in {entry.symbol for entry in repository.load_scope()}


def test_scope_carries_the_exchange(repository: StreamRepository, seeded: Session) -> None:
    by_symbol = {entry.symbol: entry.exchange for entry in repository.load_scope()}
    assert by_symbol["XU100.IS"] == "IST"
    assert by_symbol["NOEXCH"] is None


def test_scope_is_ordered(repository: StreamRepository, seeded: Session) -> None:
    """Yahoo keeps the first 100 symbols it is sent, so which ones survive
    an overflow must not vary between processes."""
    symbols = [entry.symbol for entry in repository.load_scope()]
    assert symbols == sorted(symbols)


def test_the_archive_flag_is_carried_per_symbol(
    repository: StreamRepository, seeded: Session
) -> None:
    """archive=false means quoted but not kept.

    Read off `load_scope`, which is what the writer actually consults per
    tick. The set-level `archived_symbols()` accessor this used to call had
    no other caller."""
    archived = {entry.symbol for entry in repository.load_scope() if entry.archive}
    assert "AAPL" in archived
    assert "MSFT" not in archived


def test_missing_exchange_is_counted(
    repository: StreamRepository, seeded: Session
) -> None:
    """`yfin symbols add` leaves exchange NULL until the first sync; those
    all land on one connection and the operator should be told."""
    assert repository.count_symbols_missing_exchange() == 1


# --- sessions --------------------------------------------------------------


def test_open_and_close_a_session(
    repository: StreamRepository, db_session: Session
) -> None:
    session_id = repository.open_session(connection_count=3, symbol_count=42)
    row = db_session.execute(
        text("SELECT status, connection_count, symbol_count FROM stream_sessions WHERE id = :i"),
        {"i": session_id},
    ).one()
    assert row[0] == StreamStatus.RUNNING.value
    assert (row[1], row[2]) == (3, 42)

    repository.close_session(session_id, status=StreamStatus.OK)
    closed = db_session.execute(
        text("SELECT status, finished_at FROM stream_sessions WHERE id = :i"),
        {"i": session_id},
    ).one()
    assert closed[0] == StreamStatus.OK.value
    assert closed[1] is not None


def test_stale_sessions_are_closed_as_failed(
    repository: StreamRepository, db_session: Session
) -> None:
    """A hard kill leaves the row `running` forever; without this the
    table fills with sessions that look live."""
    repository.open_session(connection_count=1, symbol_count=1)
    assert repository.close_stale_sessions() >= 1
    remaining = db_session.execute(
        text("SELECT count(*) FROM stream_sessions WHERE status = :s"),
        {"s": StreamStatus.RUNNING.value},
    ).scalar_one()
    assert remaining == 0


def test_counters_accumulate(repository: StreamRepository, db_session: Session) -> None:
    """Additive, so the writer can flush a delta without holding a total
    that a crash would lose."""
    session_id = repository.open_session(connection_count=1, symbol_count=1)
    repository.add_session_counters(session_id, messages=10, written=8, dropped=2)
    repository.add_session_counters(session_id, messages=5, written=5)
    row = db_session.execute(
        text(
            "SELECT messages_received, rows_written, rows_dropped "
            "  FROM stream_sessions WHERE id = :i"
        ),
        {"i": session_id},
    ).one()
    assert tuple(row) == (15, 13, 2)


# --- health ----------------------------------------------------------------


def _record(repository: StreamRepository, session_id: int, **overrides: object) -> None:
    fields: dict[str, object] = {
        "connection_key": "NMS",
        "state": "open",
        "subscribed_count": 96,
        "connected_at": TS,
        "last_message_at": TS,
        "last_canary_at": TS,
        "reconnect_count": 0,
        "last_error": None,
    }
    fields.update(overrides)
    repository.record_health(session_id, **fields)  # type: ignore[arg-type]


def test_health_upserts_rather_than_duplicating(
    repository: StreamRepository, db_session: Session
) -> None:
    session_id = repository.open_session(connection_count=1, symbol_count=1)
    _record(repository, session_id)
    _record(repository, session_id, state="reconnecting", reconnect_count=2)
    row = db_session.execute(
        text(
            "SELECT count(*), max(state), max(reconnect_count) "
            "  FROM stream_connection_health WHERE connection_key = 'NMS'"
        )
    ).one()
    assert row[0] == 1
    assert row[1] == "reconnecting"
    assert row[2] == 2


def test_health_rows_flag_a_stale_heartbeat(
    repository: StreamRepository, db_session: Session
) -> None:
    """The whole reason heartbeat_at exists.

    Killed hard, rows stay `open` and a dead process would otherwise read
    as healthy.
    """
    session_id = repository.open_session(connection_count=1, symbol_count=1)
    _record(repository, session_id)
    fresh = repository.health_rows(stale_after_seconds=3600)
    assert fresh[0]["stale"] is False

    db_session.execute(
        text("UPDATE stream_connection_health SET heartbeat_at = :old"),
        {"old": datetime.now(UTC) - timedelta(hours=2)},
    )
    db_session.commit()
    stale = repository.health_rows(stale_after_seconds=60)
    assert stale[0]["stale"] is True
    assert stale[0]["state"] == "open"  # still claims to be open


def test_forget_connections_removes_rows(
    repository: StreamRepository, db_session: Session
) -> None:
    """Otherwise a rebalance leaves `status` reporting a connection nobody
    is running."""
    session_id = repository.open_session(connection_count=1, symbol_count=1)
    _record(repository, session_id, connection_key="OLD")
    repository.forget_connections(["OLD"])
    assert repository.health_rows(stale_after_seconds=60) == []


def test_forget_connections_with_no_keys_is_a_no_op(
    repository: StreamRepository,
) -> None:
    repository.forget_connections([])


# --- quotes ----------------------------------------------------------------


def test_delete_quotes_removes_departed_symbols(
    repository: StreamRepository, seeded: Session
) -> None:
    seeded.execute(
        text(
            "INSERT INTO live_quotes (symbol, ts_utc, payload_hash, received_at, "
            "                         updated_at, quote_type_code, market_hours_code) "
            "VALUES ('AAPL', :ts, '0000000000000000', :ts, :ts, 8, 1)"
        ),
        {"ts": TS},
    )
    seeded.commit()
    assert repository.delete_quotes(["AAPL"]) == 1
    assert repository.delete_quotes(["AAPL"]) == 0


def test_delete_quotes_with_no_symbols_is_a_no_op(repository: StreamRepository) -> None:
    assert repository.delete_quotes([]) == 0
