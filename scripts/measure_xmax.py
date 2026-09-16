"""Does `RETURNING *, (xmax = 0) AS inserted` tell an insert from an update?

`xmax = 0` is undocumented, so it is checked on a plain table, a hypertable,
a row upserted twice in one transaction and a false `DO UPDATE ... WHERE`.
Runs in a scratch schema; re-run against a new PostgreSQL or TimescaleDB pin."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, text

from yfin.core.config import get_settings
from yfin.storage.db import create_db_engine

SCHEMA = "xmax_probe"


def _run(conn: Connection, sql: str, params: dict[str, Any], label: str) -> None:
    """Executes one statement and prints what came back."""
    print(f"  {label}: {conn.execute(text(sql), params).all()}")


def _plain(conn: Connection) -> None:
    print("1. Plain table")
    conn.execute(text("CREATE TABLE plain (k int PRIMARY KEY, v int NOT NULL)"))
    conn.commit()

    sql = (
        "INSERT INTO plain (k, v) VALUES (:k, :v) "
        "ON CONFLICT (k) DO UPDATE SET v = excluded.v "
        "RETURNING k, v, (xmax = 0) AS inserted"
    )
    _run(conn, sql, {"k": 1, "v": 10}, "first write   (expect inserted=True) ")
    conn.commit()
    _run(conn, sql, {"k": 1, "v": 20}, "second write  (expect inserted=False)")
    conn.commit()
    print()


def _hypertable(conn: Connection) -> None:
    print("2. Hypertable, through the chunk dispatch path")
    # The same shape as price_bars: a (symbol, bar_interval, ts_utc) primary
    # key with the partitioning column in it.
    conn.execute(
        text(
            "CREATE TABLE bars ("
            "  symbol text NOT NULL,"
            "  bar_interval text NOT NULL,"
            "  ts_utc timestamptz(6) NOT NULL,"
            "  close numeric(28,12),"
            "  PRIMARY KEY (symbol, bar_interval, ts_utc))"
        )
    )
    conn.execute(
        text(
            "SELECT create_hypertable('bars', by_range('ts_utc', INTERVAL '7 days'),"
            " create_default_indexes => FALSE)"
        )
    )
    conn.commit()

    sql = (
        "INSERT INTO bars (symbol, bar_interval, ts_utc, close) "
        "VALUES (:s, :i, :ts, :c) "
        "ON CONFLICT (symbol, bar_interval, ts_utc) DO UPDATE SET close = excluded.close "
        "RETURNING symbol, ts_utc, close, (xmax = 0) AS inserted"
    )
    args = {"s": "AAPL", "i": "1m", "ts": "2026-09-07T14:30:00+00:00"}
    try:
        _run(conn, sql, {**args, "c": "1.5"}, "first write   (expect inserted=True) ")
        conn.commit()
        _run(conn, sql, {**args, "c": "2.5"}, "second write  (expect inserted=False)")
        conn.commit()
        chunks = conn.execute(text("SELECT count(*) FROM show_chunks('bars')")).scalar_one()
        print("  chunks:", chunks)
    except Exception as exc:  # noqa: BLE001 - seeing the refusal IS the measurement
        conn.rollback()
        print(f"  REFUSED: {type(exc).__name__}: {exc}")
    print()


def _same_transaction(conn: Connection) -> None:
    print("3. Inserted and upserted again inside ONE transaction")
    print("   (`symbols` is written three times per symbol, by three datasets)")
    sql = (
        "INSERT INTO plain (k, v) VALUES (:k, :v) "
        "ON CONFLICT (k) DO UPDATE SET v = excluded.v "
        "RETURNING k, v, (xmax = 0) AS inserted, xmax::text, "
        "  (xmax::text::bigint = pg_current_xact_id()::text::bigint) AS xmax_is_me"
    )
    _run(conn, sql, {"k": 2, "v": 10}, "first  in tx  (expect inserted=True) ")
    _run(conn, sql, {"k": 2, "v": 20}, "second in tx  (expect inserted=False)")
    _run(conn, sql, {"k": 2, "v": 30}, "third  in tx  (expect inserted=False)")
    conn.commit()
    print()


def _guarded(conn: Connection) -> None:
    print("4. `DO UPDATE ... WHERE` with a false predicate returns no row")
    print("   (this is what makes 'a row came back' mean 'the row changed')")
    sql = (
        "INSERT INTO plain (k, v) VALUES (:k, :v) "
        "ON CONFLICT (k) DO UPDATE SET v = excluded.v "
        "  WHERE plain.v IS DISTINCT FROM excluded.v "
        "RETURNING k, v, (xmax = 0) AS inserted"
    )
    _run(conn, sql, {"k": 3, "v": 10}, "new row       (expect one row, True) ")
    conn.commit()
    _run(conn, sql, {"k": 3, "v": 10}, "unchanged     (expect [])            ")
    conn.commit()
    _run(conn, sql, {"k": 3, "v": 11}, "changed       (expect one row, False)")
    conn.commit()

    print("   DO NOTHING on an existing row:")
    nothing = (
        "INSERT INTO plain (k, v) VALUES (:k, :v) "
        "ON CONFLICT (k) DO NOTHING RETURNING k, (xmax = 0) AS inserted"
    )
    _run(conn, nothing, {"k": 3, "v": 99}, "existing      (expect [])            ")
    _run(conn, nothing, {"k": 4, "v": 99}, "new           (expect one row, True) ")
    conn.commit()
    print()


def main() -> int:
    engine = create_db_engine(get_settings())
    with engine.connect() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {SCHEMA}"))
        # The extension installs into public and by_range() is unqualified.
        conn.execute(text(f"SET search_path TO {SCHEMA}, public"))
        conn.commit()

        print(conn.execute(text("SELECT version()")).scalar_one())
        version = conn.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
        ).scalar_one_or_none()
        print("timescaledb", version)
        print()

        try:
            _plain(conn)
            _hypertable(conn)
            _same_transaction(conn)
            _guarded(conn)
        finally:
            conn.rollback()
            conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
            conn.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
