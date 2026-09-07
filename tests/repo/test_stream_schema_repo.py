"""Stream tables against a real PostgreSQL + TimescaleDB.

The unit tests check what the models declare; these check what the
database actually does with them. Three behaviours can only be verified
here: hypertable partitioning, the dedupe semantics of the three-column
primary key, and that the foreign keys refuse what they are supposed to.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from yfin.models.stream import (
    LiveTick,
    StreamOutbox,
    StreamRejectReason,
    StreamRelayOffset,
    StreamScope,
)

pytestmark = pytest.mark.repo

TS = datetime(2026, 9, 7, 14, 30, tzinfo=UTC)


def _seed_symbol(session: Session, symbol: str = "AAPL") -> None:
    session.execute(
        text(
            "INSERT INTO symbols (symbol, is_active, unknown_streak, created_at, updated_at) "
            "VALUES (:s, true, 0, now(), now()) ON CONFLICT (symbol) DO NOTHING"
        ),
        {"s": symbol},
    )


def _tick(
    *,
    symbol: str = "AAPL",
    ts: datetime = TS,
    payload_hash: str = "0" * 16,
    price: str = "232.35",
    unknown_fields: str | None = None,
) -> LiveTick:
    return LiveTick(
        symbol=symbol,
        ts_utc=ts,
        payload_hash=payload_hash,
        received_at=ts,
        price=Decimal(price),
        quote_type_code=8,
        market_hours_code=1,
        unknown_fields=unknown_fields,
    )


def _insert_tick(session: Session, tick: LiveTick) -> None:
    """Insert with ON CONFLICT DO NOTHING, the way the writer does."""
    columns = {c.name: getattr(tick, c.name) for c in LiveTick.__table__.c}
    names = ", ".join(columns)
    binds = ", ".join(f":{name}" for name in columns)
    session.execute(
        text(
            f"INSERT INTO live_ticks ({names}) VALUES ({binds}) "
            f"ON CONFLICT (symbol, ts_utc, payload_hash) DO NOTHING"
        ),
        columns,
    )


# --- hypertables -----------------------------------------------------------


@pytest.mark.parametrize(
    ("table", "column", "interval"),
    [("live_ticks", "ts_utc", timedelta(days=1)),
     ("stream_outbox", "created_at", timedelta(hours=1))],
)
def test_stream_tables_are_hypertables(
    db_session: Session, table: str, column: str, interval: timedelta
) -> None:
    """Without the explicit DDL these would be plain tables.

    create_all() knows nothing about hypertables and Alembic cannot
    autogenerate them, so this is the only thing standing between the
    design and a silently unpartitioned archive.
    """
    row = db_session.execute(
        text(
            "SELECT column_name, time_interval FROM timescaledb_information.dimensions "
            "WHERE hypertable_name = :t AND hypertable_schema = current_schema()"
        ),
        {"t": table},
    ).one()
    assert row[0] == column
    assert row[1] == interval


def test_writing_a_tick_creates_a_chunk(db_session: Session) -> None:
    _seed_symbol(db_session)
    _insert_tick(db_session, _tick())
    db_session.flush()
    chunks = db_session.execute(
        text(
            "SELECT count(*) FROM timescaledb_information.chunks "
            "WHERE hypertable_name = 'live_ticks' "
            "AND hypertable_schema = current_schema()"
        )
    ).scalar_one()
    assert chunks >= 1


# --- the dedupe triple -----------------------------------------------------


def test_identical_message_twice_stores_one_row(db_session: Session) -> None:
    """Yahoo re-sends snapshots; the archive must not grow on repeats."""
    _seed_symbol(db_session)
    _insert_tick(db_session, _tick())
    _insert_tick(db_session, _tick())
    db_session.flush()
    assert db_session.scalar(select(func.count()).select_from(LiveTick)) == 1


def test_two_ticks_in_the_same_millisecond_are_both_kept(db_session: Session) -> None:
    """The reason ts_utc alone cannot be the key.

    Yahoo can emit several messages for one symbol inside a millisecond.
    Keyed on (symbol, ts_utc) the second one would silently replace or
    collide with the first.
    """
    _seed_symbol(db_session)
    _insert_tick(db_session, _tick(payload_hash="a" * 16, price="232.35"))
    _insert_tick(db_session, _tick(payload_hash="b" * 16, price="232.40"))
    db_session.flush()
    assert db_session.scalar(select(func.count()).select_from(LiveTick)) == 2


def test_a_new_proto_field_is_not_deduped_away(db_session: Session) -> None:
    """unknown_fields is part of payload_hash, and this is why.

    Two messages whose 33 known fields agree but which differ in a field
    this build does not map yet must both be stored -- otherwise the one
    piece of evidence that pricing.proto moved is the one thing dropped.
    """
    _seed_symbol(db_session)
    _insert_tick(db_session, _tick(payload_hash="c" * 16, unknown_fields=None))
    _insert_tick(db_session, _tick(payload_hash="d" * 16, unknown_fields='{"new_field":"7"}'))
    db_session.flush()
    assert db_session.scalar(select(func.count()).select_from(LiveTick)) == 2


# --- foreign keys ----------------------------------------------------------


def test_tick_for_an_unknown_symbol_is_refused(db_session: Session) -> None:
    """Why the writer filters through known_symbols() before writing.

    One unknown symbol aborts the whole statement, so a 500-row batch
    would be lost to a single stray ticker.
    """
    # A savepoint, not a plain rollback: the session fixture wraps each
    # test in its own transaction, and rolling that back from inside the
    # test leaves the fixture with nothing to release.
    with pytest.raises(IntegrityError), db_session.begin_nested():
        _insert_tick(db_session, _tick(symbol="NOPE"))
        db_session.flush()


def test_outbox_accepts_a_symbol_that_is_not_in_symbols(db_session: Session) -> None:
    """Deliberately FK-free: a transient publish queue should not pay for
    a shared lock on `symbols` on every insert."""
    db_session.add(
        StreamOutbox(created_at=TS, symbol="NOPE", exchange="NMS", payload="{}")
    )
    db_session.flush()


def test_reject_row_survives_for_an_unknown_symbol(db_session: Session) -> None:
    """The whole point of stream_rejects having no FK.

    An `unknown_symbol` reject is by definition about a symbol that is
    not in `symbols`; with a foreign key the record of the rejection
    would itself be rejected.
    """
    db_session.execute(
        text(
            "INSERT INTO stream_rejects (received_at, symbol, reason, detail) "
            "VALUES (:ts, :s, :r, :d)"
        ),
        {"ts": TS, "s": "NOPE", "r": StreamRejectReason.UNKNOWN_SYMBOL.value,
         "d": "not in universe"},
    )
    db_session.flush()
    stored = db_session.execute(
        text("SELECT symbol, reason FROM stream_rejects WHERE symbol = 'NOPE'")
    ).one()
    assert stored[1] == StreamRejectReason.UNKNOWN_SYMBOL.value


# --- constraints -----------------------------------------------------------


def test_relay_offset_allows_only_one_row(db_session: Session) -> None:
    """A second offset row would mean two relays not seeing each other."""
    db_session.add(StreamRelayOffset(id=1, last_published_id=0, updated_at=TS))
    db_session.flush()
    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.add(StreamRelayOffset(id=2, last_published_id=0, updated_at=TS))
        db_session.flush()


def test_scope_defaults_to_enabled_and_archived(db_session: Session) -> None:
    _seed_symbol(db_session, "MSFT")
    db_session.execute(
        text("INSERT INTO stream_scope (symbol, added_at) VALUES ('MSFT', :ts)"),
        {"ts": TS},
    )
    db_session.flush()
    db_session.expire_all()
    scope = db_session.get(StreamScope, "MSFT")
    assert scope is not None
    assert scope.enabled is True
    assert scope.archive is True


def test_enum_type_rejects_an_unknown_reason(db_session: Session) -> None:
    """The reject reasons are a closed set in the database too."""
    with pytest.raises(DBAPIError), db_session.begin_nested():
        db_session.execute(
            text(
                "INSERT INTO stream_rejects (received_at, reason) "
                "VALUES (:ts, 'not_a_reason')"
            ),
            {"ts": TS},
        )
        db_session.flush()
