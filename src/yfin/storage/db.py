"""Engine, session factory, and PostgreSQL advisory lock."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from yfin.core.config import Settings, get_settings

SYNC_LOCK_NAME = "yfin_sync"


def _lock_key(name: str) -> int:
    """Name -> signed 64-bit advisory lock key.

    Not PostgreSQL's `hashtext()`: its output can change between server versions.
    """
    digest = hashlib.blake2b(name.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def _lock_key_parts(key: int) -> tuple[int, int]:
    """`pg_locks.classid` / `objid` are both `oid`, unsigned 32-bit.

    Split here, not in SQL: `::int` overflows past 2^31 and `>>` on a
    negative key sign-extends instead of yielding the top 32 bits.
    """
    unsigned = key & 0xFFFF_FFFF_FFFF_FFFF
    return unsigned >> 32, unsigned & 0xFFFF_FFFF


def create_db_engine(
    settings: Settings | None = None,
    database: str | None = None,
    *,
    schema: str | None = None,
    pool_size: int | None = None,
    application_name: str = "yfin",
) -> Engine:
    """Pool is sized for the concurrent consumers of one process.

    The symbol transaction, three read-only providers, and the lock session.
    """
    cfg = settings or get_settings()
    # Per-process ceiling is pool_size + max_overflow = 2 x pool_size, so
    # N shards need (N x 2 x pool_size) + 2 <= PostgreSQL max_connections.
    if pool_size is None:
        pool_size = max(5, cfg.yf_max_workers + 4)

    # `options` is a single string: timezone and search_path as separate
    # connect_args keys would have one overwrite the other.
    options = ["-c timezone=UTC"]
    if schema is not None:
        # `public` is required: the timescaledb extension lives there, and
        # without it `create_hypertable` fails to resolve.
        options.append(f"-c search_path={schema},public")

    return create_engine(
        cfg.db_url(database),
        pool_pre_ping=True,
        pool_recycle=3600,
        pool_size=pool_size,
        max_overflow=pool_size,
        pool_timeout=60,
        future=True,
        connect_args={
            # Makes lock ownership diagnosable. Shards append their own
            # index (`yfin-shard-2`).
            "application_name": application_name,
            "options": " ".join(options),
        },
    )


def session_factory(engine: Engine) -> sessionmaker[Session]:
    """The one session shape this process uses.

    `expire_on_commit=False` is load-bearing: the pipeline reads ORM
    attributes after committing, sometimes after the session is closed.
    """
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def rowcount(result: Any) -> int:
    """Rows affected by a DML statement (`rowcount` exists only on `CursorResult`).

    Not usable as write verification: `ON CONFLICT DO NOTHING` reports 0
    for skipped rows. Fine for a DELETE.
    """
    return int(getattr(result, "rowcount", 0) or 0)


def returns_rows(result: Any) -> bool:
    """Whether the statement just executed had a RETURNING clause.

    A write whose update map is entirely volatile returns nothing, and
    reading that result raises `ResourceClosedError`.
    """
    return bool(getattr(result, "returns_rows", False))


class LockNotAcquired(RuntimeError):
    """Advisory lock could not be acquired; another sync is running."""


_HOLDER_SQL = text(
    "SELECT a.pid, a.application_name, a.client_addr, a.state, "
    "       left(a.query, 120) AS query "
    "  FROM pg_locks l "
    "  JOIN pg_stat_activity a ON a.pid = l.pid "
    " WHERE l.locktype = 'advisory' "
    "   AND l.classid = :classid AND l.objid = :objid "
    # objsubid = 1 is the single-bigint form; the two-int form uses 2.
    # The wrong objsubid makes the query silently return empty.
    "   AND l.objsubid = 1 AND l.granted"
)


def lock_holder(conn: Any, name: str = SYNC_LOCK_NAME) -> str | None:
    """Human-readable description of the lock holder session (None if unlocked)."""
    classid, objid = _lock_key_parts(_lock_key(name))
    row = conn.execute(_HOLDER_SQL, {"classid": classid, "objid": objid}).first()
    if row is None:
        return None
    return (
        f"pid={row.pid} application_name={row.application_name!r} "
        f"client={row.client_addr} state={row.state} query={row.query!r}"
    )


@contextmanager
def advisory_lock(engine: Engine, name: str = SYNC_LOCK_NAME) -> Iterator[None]:
    """pg_try_advisory_lock; raises LockNotAcquired if unavailable.

    Advisory locks are per-database and session-scoped: the same
    connection is held open for the duration.
    """
    key = _lock_key(name)
    conn = engine.connect()
    try:
        got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar()
        if not got:
            # Include who holds it. A bare "not acquired" message tells
            # someone who accidentally started two overlapping runs nothing.
            holder = lock_holder(conn, name) or "holder not found"
            raise LockNotAcquired(
                f"advisory lock not acquired: {name} ({holder}). "
                "Another sync or live test run may still be in progress."
            )
        try:
            yield
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
    finally:
        conn.close()
