"""How a relay walks its queue, and why the two ways are not the same.

The tick outbox walks row ids. That is correct only because its writer is a
single thread, so `id` follows commit order and a row with a low id can
never appear after one with a high id. `stream/writer.py` states that
premise; this module is where it is depended on.

The pipeline outbox cannot claim it. Symbol transactions commit
concurrently -- N shard processes times worker threads -- so a transaction
can take its ids early and commit late, and an `id`-ordered walk would step
straight past its rows. Those rows exist nowhere else, so that is silent
data loss, which is the one failure mode this whole subsystem exists to
prevent.

So there are two cursors, and they are separate classes rather than five
`if spec.cursor` branches inside the relay: each one's correctness argument
belongs next to its own SQL, and the relay has no business knowing which of
the two it is driving.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

from yfin.outbox.kafka import OutboxMessage
from yfin.outbox.spec import OutboxSpec

#: How far behind `now()` the xid cursor's chunk cleanup is allowed to reach.
#:
#: `drop_chunks(older_than => c)` drops a chunk only when its whole range
#: ends at or before `c`. With one-hour chunks not aligned to `now()`,
#: `now() - 3h` guarantees any chunk dropped closed at least two hours ago.
#: The exposure that leaves is a transaction still open more than two hours
#: after its flush -- which the flush being the last statement before the
#: commit rules out short of a pathological lock wait, and
#: `persist_with_retry` logs that case.
CLEANUP_LAG_HOURS = 3


@dataclass(frozen=True)
class Position:
    """How far a relay has published.

    A PAIR even for the id cursor, which leaves `xid` at zero. One shape
    beats two, and the id cursor simply never reads the field.
    """

    xid: int
    id: int


@dataclass(frozen=True)
class Lag:
    """What `yfin ... status` reports."""

    #: Rows the relay has not published yet.
    rows: int
    #: Age of the oldest of them, in seconds.
    oldest_age_seconds: int
    #: Age of the oldest OPEN WRITING transaction, in seconds, or None where
    #: the cursor does not wait on one. A growing `rows` with this set means
    #: the relay is not behind, it is blocked -- and the operator needs to
    #: know which, because the two have different fixes.
    held_back_seconds: int | None = None


class Cursor(Protocol):
    """The five things that differ between walking ids and walking xids."""

    def read(self, session: Session, spec: OutboxSpec) -> Position: ...
    def advance(self, session: Session, spec: OutboxSpec, last: Position) -> None: ...
    def batch(
        self, session: Session, spec: OutboxSpec, at: Position, limit: int
    ) -> list[OutboxMessage]: ...
    def cutoff(
        self, session: Session, spec: OutboxSpec, at: Position
    ) -> datetime | None: ...
    def lag(self, session: Session, spec: OutboxSpec, at: Position) -> Lag: ...


def _ensure_offset_row(session: Session, spec: OutboxSpec) -> None:
    """Creates the single offset row on first use.

    One statement for both tables: every published-position column on both
    carries a server default of zero, so only `updated_at` has to be
    supplied. Zero is below every real id and every real transaction id, so
    a relay starting on a non-empty outbox publishes all of it rather than
    skipping to the end.

    No migration seeds this row. The repo fixtures build the schema from
    `Base.metadata` rather than by running migrations, so a seeded row would
    exist in production and not in the tests.
    """
    session.execute(
        text(
            f"INSERT INTO {spec.offset_table} (id, updated_at) "
            "VALUES (1, :ts) ON CONFLICT (id) DO NOTHING"
        ),
        {"ts": datetime.now(UTC)},
    )
    session.commit()


class IdCursor:
    """Walks `id`, for a queue with a single writer thread.

    Every query here is the one the tick relay has always run; the class is
    a home for them, not a change to them.
    """

    def read(self, session: Session, spec: OutboxSpec) -> Position:
        row = session.execute(
            text(f"SELECT last_published_id FROM {spec.offset_table} WHERE id = 1")
        ).scalar_one_or_none()
        if row is None:
            _ensure_offset_row(session, spec)
            return Position(xid=0, id=0)
        return Position(xid=0, id=int(row))

    def advance(self, session: Session, spec: OutboxSpec, last: Position) -> None:
        session.execute(
            text(
                f"UPDATE {spec.offset_table} "
                "   SET last_published_id = :id, updated_at = :ts "
                " WHERE id = 1"
            ),
            {"id": last.id, "ts": datetime.now(UTC)},
        )
        session.commit()

    def batch(
        self, session: Session, spec: OutboxSpec, at: Position, limit: int
    ) -> list[OutboxMessage]:
        rows = session.execute(
            text(
                f"SELECT id, {spec.key_column}, {spec.route_column}, payload "
                f"  FROM {spec.table} "
                " WHERE id > :offset ORDER BY id LIMIT :limit"
            ),
            {"offset": at.id, "limit": limit},
        ).all()
        return [
            OutboxMessage(id=r[0], xid=None, key=r[1], route=r[2], payload=r[3])
            for r in rows
        ]

    def cutoff(
        self, session: Session, spec: OutboxSpec, at: Position
    ) -> datetime | None:
        """The `created_at` of the oldest UNPUBLISHED row.

        Safe because `created_at` is generated by the writer in `id` order:
        a late tick cannot appear with a low `created_at` and a high `id`.
        """
        value = session.execute(
            text(f"SELECT min(created_at) FROM {spec.table} WHERE id > :offset"),
            {"offset": at.id},
        ).scalar_one_or_none()
        if value is None:
            # Everything is published; the newest row's timestamp is the
            # boundary.
            value = session.execute(
                text(f"SELECT max(created_at) FROM {spec.table}")
            ).scalar_one_or_none()
        return value

    def lag(self, session: Session, spec: OutboxSpec, at: Position) -> Lag:
        row = session.execute(
            text(
                "SELECT count(*), "
                "       COALESCE(EXTRACT(EPOCH FROM (now() - min(created_at)))::bigint, 0) "
                f"  FROM {spec.table} WHERE id > :offset"
            ),
            {"offset": at.id},
        ).one()
        return Lag(rows=int(row[0]), oldest_age_seconds=int(row[1]))


class XidCursor:
    """Walks `(xid, id)`, for a queue written by concurrent transactions.

    A row becomes eligible only once `xid < pg_snapshot_xmin(...)`, i.e.
    once every transaction with a smaller id has ENDED -- committed or
    aborted, and an aborted one's rows are invisible anyway. Every
    transaction that starts later takes a higher id. So a transaction that
    took its ids early and commits late is waited for rather than skipped,
    and once its xid is below xmin all of its rows are visible at once.

    The cursor is the PAIR. A bare xid could neither advance past a
    transaction larger than one batch nor resume inside it, so one big
    backfill would stall the relay indefinitely.

    The price of the guarantee is that the relay cannot pass an open writing
    transaction ANYWHERE in the database -- a long bar backfill, a `psql`
    session left idle in transaction. The outbox window itself is small (the
    flush is the last statement before the commit), but the wait is on the
    oldest WRITER, not the oldest flush. `lag` reports that separately so an
    operator can tell "behind" from "blocked".
    """

    def read(self, session: Session, spec: OutboxSpec) -> Position:
        row = session.execute(
            text(
                "SELECT last_published_xid::text, last_published_id "
                f"  FROM {spec.offset_table} WHERE id = 1"
            )
        ).one_or_none()
        if row is None:
            _ensure_offset_row(session, spec)
            return Position(xid=0, id=0)
        return Position(xid=int(row[0]), id=int(row[1]))

    def advance(self, session: Session, spec: OutboxSpec, last: Position) -> None:
        session.execute(
            text(
                f"UPDATE {spec.offset_table} "
                "   SET last_published_xid = CAST(:xid AS xid8), "
                "       last_published_id = :id, "
                "       updated_at = :ts "
                " WHERE id = 1"
            ),
            {"xid": str(last.xid), "id": last.id, "ts": datetime.now(UTC)},
        )
        session.commit()

    def batch(
        self, session: Session, spec: OutboxSpec, at: Position, limit: int
    ) -> list[OutboxMessage]:
        rows = session.execute(
            text(
                f"SELECT id, xid::text, {spec.key_column}, {spec.route_column}, payload "
                f"  FROM {spec.table} "
                " WHERE (xid, id) > (CAST(:last_xid AS xid8), :last_id) "
                "   AND xid < pg_snapshot_xmin(pg_current_snapshot()) "
                " ORDER BY xid, id LIMIT :limit"
            ),
            {"last_xid": str(at.xid), "last_id": at.id, "limit": limit},
        ).all()
        return [
            OutboxMessage(id=r[0], xid=int(r[1]), key=r[2], route=r[3], payload=r[4])
            for r in rows
        ]

    def cutoff(
        self, session: Session, spec: OutboxSpec, at: Position
    ) -> datetime | None:
        """`LEAST(oldest unpublished, now() - 3 chunk intervals)`.

        The first term is exact: rows of transactions at or below the cursor
        are all published, and rows of transactions still open are invisible
        to `min`. The second is what covers the chunks a still-open
        transaction could yet flush into -- see `CLEANUP_LAG_HOURS`.

        PostgreSQL's `LEAST` ignores NULL, so with nothing unpublished the
        second term stands on its own, which is the intended answer.
        """
        return session.execute(
            text(
                "SELECT LEAST("
                f"  (SELECT min(created_at) FROM {spec.table} "
                "    WHERE (xid, id) > (CAST(:last_xid AS xid8), :last_id)), "
                "  now() - make_interval(hours => :lag)"
                ")"
            ),
            {"last_xid": str(at.xid), "last_id": at.id, "lag": CLEANUP_LAG_HOURS},
        ).scalar_one_or_none()

    def lag(self, session: Session, spec: OutboxSpec, at: Position) -> Lag:
        row = session.execute(
            text(
                "SELECT count(*), "
                "       COALESCE(EXTRACT(EPOCH FROM (now() - min(created_at)))::bigint, 0) "
                f"  FROM {spec.table} "
                " WHERE (xid, id) > (CAST(:last_xid AS xid8), :last_id)"
            ),
            {"last_xid": str(at.xid), "last_id": at.id},
        ).one()
        held = session.execute(
            text(
                "SELECT EXTRACT(EPOCH FROM (now() - min(xact_start)))::bigint "
                "  FROM pg_stat_activity WHERE backend_xid IS NOT NULL"
            )
        ).scalar_one_or_none()
        return Lag(
            rows=int(row[0]),
            oldest_age_seconds=int(row[1]),
            held_back_seconds=None if held is None else int(held),
        )


def cursor_for(spec: OutboxSpec) -> Cursor:
    return XidCursor() if spec.cursor == "xid" else IdCursor()


__all__ = ["CLEANUP_LAG_HOURS", "Cursor", "IdCursor", "Lag", "Position", "XidCursor", "cursor_for"]
