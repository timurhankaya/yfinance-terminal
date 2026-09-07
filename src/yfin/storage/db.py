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

    Not PostgreSQL's `hashtext()`: it's an undocumented internal function
    and its output can change between versions. The key must be
    version-independent so two clients on different server versions see
    the same lock.
    """
    digest = hashlib.blake2b(name.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def _lock_key_parts(key: int) -> tuple[int, int]:
    """`pg_locks.classid` / `objid` are both `oid`, unsigned 32-bit.

    Splitting happens here, not in SQL. `(:key >> 32)::int` in SQL would
    fail two ways:
      1. `::int` raises ERROR 22003 once the low 32 bits exceed 2^31.
      2. `>>` on a negative key is an arithmetic shift: it sign-extends
         and never yields the top 32 bits.
    The 'yfin_sync' key hits both cases (measured: key is negative,
    classid = 3,089,743,290), so this isn't theoretical -- it would
    break on first use of the project's only advisory lock, and since
    that's inside the LockNotAcquired error path, it would mask the
    real error.
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
    """Pool is sized for concurrent consumers.

    Concurrent connection requesters: the main thread's symbol
    transaction, three read-only providers (watermark, scope, gap), and
    the session holding the advisory lock. The old default pool_size=5
    was right at that total and could produce QueuePool timeouts.
    """
    cfg = settings or get_settings()
    # Shards set pool_size explicitly: real concurrent consumers per
    # process are four. The three read-only providers (WatermarkReader,
    # ScopeReader, GapReader) each serialize their own reads with a
    # threading.Lock, so each holds at most one connection, plus the
    # main thread's symbol transaction.
    # Invariant: per-process ceiling is pool_size + max_overflow, i.e.
    # 2 x pool_size. Default pool_size = max(5, workers+4) = 8
    # (workers=4), so at most 16 per shard, (N x 16) + 2 for N shards.
    # PostgreSQL max_connections must be sized accordingly
    # (docker-compose.yml: 200).
    if pool_size is None:
        pool_size = max(5, cfg.yf_max_workers + 4)

    # `options` is a single string: timezone and search_path as separate
    # connect_args keys would have one overwrite the other.
    options = ["-c timezone=UTC"]
    if schema is not None:
        # `public` is required: the timescaledb extension lives there,
        # and without it `create_hypertable` / `timescaledb_information.*`
        # fail to resolve ("function by_range(unknown, interval) does
        # not exist" -- measured). Tests would silently fall back to a
        # plain table without noticing.
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

    There were eleven `sessionmaker(...)` calls and four different shapes
    among them: six passed `expire_on_commit=False, future=True`, three in
    `stream/runner.py` left `future` off, and one in the CLI passed neither
    -- so whether an object was still readable after `commit()` depended on
    which file had opened the session.

    `expire_on_commit=False` is the load-bearing half. The pipeline reads
    attributes off ORM objects after committing (the audit path does this
    on every run), and the default would re-query for each one, or fail
    outright once the session is closed.
    """
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def rowcount(result: Any) -> int:
    """Rows affected by a DML statement.

    `Session.execute` is statically typed to return `Result`, and `rowcount`
    only exists on `CursorResult`. Written once here rather than as a
    `# type: ignore[attr-defined]` at each call site, which is what three
    different modules had grown.

    Not usable as verification -- `ON CONFLICT DO NOTHING` reports 0 for
    rows it skipped (measured: `INSERT 0 0`), which is why writes are
    verified by an independent key-existence read. It is fine for a DELETE,
    where "how many did I remove" is exactly what it answers.
    """
    return int(getattr(result, "rowcount", 0) or 0)


class LockNotAcquired(RuntimeError):
    """Advisory lock could not be acquired; another sync is running."""


_HOLDER_SQL = text(
    "SELECT a.pid, a.application_name, a.client_addr, a.state, "
    "       left(a.query, 120) AS query "
    "  FROM pg_locks l "
    "  JOIN pg_stat_activity a ON a.pid = l.pid "
    " WHERE l.locktype = 'advisory' "
    "   AND l.classid = :classid AND l.objid = :objid "
    # objsubid = 1 is the single-bigint form; the two-int form uses 2
    # (measured). Wrong objsubid makes the query silently return empty.
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

    No `timeout` parameter: PostgreSQL's equivalent isn't a duration in
    seconds but a binary choice -- `pg_advisory_lock` (wait forever) or
    `pg_try_advisory_lock` (don't wait at all). Carrying a parameter that
    implies otherwise would be misleading.

    Scope: PostgreSQL advisory locks are per-database, so two runs
    against different databases never see each other's lock. Live tests
    and `run_sync` must use the same database.

    The lock is session-scoped, so the same connection is held open for
    the duration.
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
