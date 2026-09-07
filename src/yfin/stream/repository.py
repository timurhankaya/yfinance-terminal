"""Every read and maintenance query the stream package makes.

The write path goes through `storage/contracts.py` like the rest of the
codebase, but that contract only offers `write` / `current_hash` /
`known_symbols` -- there is no general SELECT in it. The scope join, the
session bookkeeping and the health table need one, so the SQL lives here
rather than leaking into the supervisor or the CLI.

Nothing in this module knows about sockets or asyncio. The supervisor
calls it from a worker thread, never from the event loop.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, sessionmaker

from yfin.models.stream import StreamStatus

#: Advisory lock names. Separate from `yfin_sync` so a stream process and
#: a scheduled sync can run at the same time -- they write different
#: tables. `yfin stream reconcile` is the exception and takes the sync
#: lock, because it writes price_bars.
STREAM_LOCK_NAME = "yfin_stream"
RELAY_LOCK_NAME = "yfin_stream_relay"


@dataclass(frozen=True)
class ScopeEntry:
    """One symbol in the streaming universe."""

    symbol: str
    exchange: str | None
    archive: bool


class StreamRepository:
    """Reads and writes the stream tables that are not the tick path."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    # --- scope -------------------------------------------------------------

    def load_scope(self) -> list[ScopeEntry]:
        """The symbols to subscribe to, with their exchange.

        Both conditions are required. `stream_scope.enabled` is the
        operator's choice; `symbols.is_active` is the pipeline's. Without
        the second one a symbol that was deactivated by hand, or that
        crossed the delist threshold, would keep streaming forever --
        `known_symbols()` only checks that the row exists, not that it is
        active, so nothing further down would catch it.

        Ordered so two processes reading the same universe produce the
        same subscription: Yahoo keeps the first 100 entries it is sent,
        so ordering decides which symbols survive an overflow.
        """
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    "SELECT sc.symbol, s.exchange, sc.archive "
                    "  FROM stream_scope sc "
                    "  JOIN symbols s ON s.symbol = sc.symbol "
                    " WHERE sc.enabled AND s.is_active "
                    " ORDER BY sc.symbol"
                )
            ).all()
        return [ScopeEntry(symbol=r[0], exchange=r[1], archive=r[2]) for r in rows]

    def archived_symbols(self) -> set[str]:
        """Symbols whose ticks are kept, as opposed to only quoted.

        `archive = false` keeps a symbol on the wire and in live_quotes
        but out of live_ticks. That is the volume dial: the archive runs
        to ~500-600 GB/year at 500 symbols, and some symbols are wanted
        live without their history being wanted at all.
        """
        return {entry.symbol for entry in self.load_scope() if entry.archive}

    def count_symbols_missing_exchange(self) -> int:
        """Scoped symbols whose exchange is still NULL.

        `yfin symbols add` writes only the symbol and is_active, so
        exchange stays NULL until the first sync. Those symbols stream
        fine but all land on the `unknown` connection, and an operator
        should be told rather than discover it in `stream status`.
        """
        with self._session_factory() as session:
            return int(
                session.execute(
                    text(
                        "SELECT count(*) FROM stream_scope sc "
                        "  JOIN symbols s ON s.symbol = sc.symbol "
                        " WHERE sc.enabled AND s.is_active AND s.exchange IS NULL"
                    )
                ).scalar_one()
            )

    # --- sessions ----------------------------------------------------------

    def open_session(self, *, connection_count: int, symbol_count: int) -> int:
        """Starts a stream_sessions row and returns its id."""
        with self._session_factory() as session:
            session_id = session.execute(
                text(
                    "INSERT INTO stream_sessions "
                    "  (started_at, status, connection_count, symbol_count) "
                    "VALUES (:ts, :status, :connections, :symbols) "
                    "RETURNING id"
                ),
                {
                    "ts": datetime.now(UTC),
                    "status": StreamStatus.RUNNING.value,
                    "connections": connection_count,
                    "symbols": symbol_count,
                },
            ).scalar_one()
            session.commit()
        return int(session_id)

    def close_session(self, session_id: int, *, status: StreamStatus) -> None:
        with self._session_factory() as session:
            session.execute(
                text(
                    "UPDATE stream_sessions SET finished_at = :ts, status = :status "
                    " WHERE id = :id"
                ),
                {"ts": datetime.now(UTC), "status": status.value, "id": session_id},
            )
            session.commit()

    def close_stale_sessions(self) -> int:
        """Closes sessions a previous process never finished.

        A hard kill leaves the row `running` forever. Left alone the table
        would fill with sessions that look live, and nothing would
        distinguish the current process from three crashed ones.
        """
        with self._session_factory() as session:
            result = session.execute(
                text(
                    "UPDATE stream_sessions SET finished_at = :ts, status = :status "
                    " WHERE status = :running"
                ),
                {
                    "ts": datetime.now(UTC),
                    "status": StreamStatus.FAILED.value,
                    "running": StreamStatus.RUNNING.value,
                },
            )
            session.commit()
            # CursorResult, not Result: rowcount only exists on the DML
            # form and mypy cannot narrow it from execute()'s signature.
            return int(cast("CursorResult[Any]", result).rowcount or 0)

    def add_session_counters(
        self,
        session_id: int,
        *,
        messages: int = 0,
        written: int = 0,
        rejected: int = 0,
        dropped: int = 0,
    ) -> None:
        """Accumulates counters onto the session row.

        Additive rather than absolute so the writer can flush whatever it
        has since the last batch without holding a running total that a
        crash would lose.
        """
        with self._session_factory() as session:
            session.execute(
                text(
                    "UPDATE stream_sessions SET "
                    "  messages_received = messages_received + :messages, "
                    "  rows_written = rows_written + :written, "
                    "  rows_rejected = rows_rejected + :rejected, "
                    "  rows_dropped = rows_dropped + :dropped "
                    " WHERE id = :id"
                ),
                {
                    "messages": messages,
                    "written": written,
                    "rejected": rejected,
                    "dropped": dropped,
                    "id": session_id,
                },
            )
            session.commit()

    # --- connection health -------------------------------------------------

    def record_health(
        self,
        session_id: int,
        *,
        connection_key: str,
        state: str,
        subscribed_count: int,
        connected_at: datetime | None,
        last_message_at: datetime | None,
        last_canary_at: datetime | None,
        reconnect_count: int,
        last_error: str | None,
    ) -> None:
        """Upserts one connection's live state.

        `heartbeat_at` is set here, on every write, and it is what stops
        this table from lying after a crash: killed hard, the rows would
        stay `open` and `yfin stream status` would report a dead process
        as healthy.
        """
        with self._session_factory() as session:
            session.execute(
                text(
                    "INSERT INTO stream_connection_health "
                    "  (connection_key, session_id, state, subscribed_count, "
                    "   connected_at, last_message_at, last_canary_at, "
                    "   heartbeat_at, reconnect_count, last_error) "
                    "VALUES (:key, :session_id, :state, :subscribed, "
                    "        :connected_at, :last_message_at, :last_canary_at, "
                    "        :heartbeat, :reconnects, :error) "
                    "ON CONFLICT (connection_key) DO UPDATE SET "
                    "  session_id = excluded.session_id, "
                    "  state = excluded.state, "
                    "  subscribed_count = excluded.subscribed_count, "
                    "  connected_at = excluded.connected_at, "
                    "  last_message_at = excluded.last_message_at, "
                    "  last_canary_at = excluded.last_canary_at, "
                    "  heartbeat_at = excluded.heartbeat_at, "
                    "  reconnect_count = excluded.reconnect_count, "
                    "  last_error = excluded.last_error"
                ),
                {
                    "key": connection_key,
                    "session_id": session_id,
                    "state": state,
                    "subscribed": subscribed_count,
                    "connected_at": connected_at,
                    "last_message_at": last_message_at,
                    "last_canary_at": last_canary_at,
                    "heartbeat": datetime.now(UTC),
                    "reconnects": reconnect_count,
                    "error": last_error,
                },
            )
            session.commit()

    def forget_connections(self, keys: Sequence[str]) -> None:
        """Drops health rows for connections that no longer exist.

        Without this a rebalance leaves rows behind and `stream status`
        keeps reporting a connection nobody is running.
        """
        if not keys:
            return
        with self._session_factory() as session:
            session.execute(
                text("DELETE FROM stream_connection_health WHERE connection_key = ANY(:keys)"),
                {"keys": list(keys)},
            )
            session.commit()

    def health_rows(self, *, stale_after_seconds: float) -> list[dict[str, object]]:
        """Connection health for `yfin stream status`, with staleness.

        A row is `stale` when its heartbeat is older than the caller's
        threshold. Reporting that separately from `state` matters: a
        crashed process leaves rows saying `open`, and only the heartbeat
        age reveals that nothing is actually running.
        """
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    "SELECT connection_key, state, subscribed_count, connected_at, "
                    "       last_message_at, last_canary_at, heartbeat_at, "
                    "       reconnect_count, last_error, "
                    "       (now() - heartbeat_at) > make_interval(secs => :stale) AS stale "
                    "  FROM stream_connection_health "
                    " ORDER BY connection_key"
                ),
                {"stale": stale_after_seconds},
            ).mappings().all()
        return [dict(row) for row in rows]

    # --- quotes ------------------------------------------------------------

    def delete_quotes(self, symbols: Sequence[str]) -> int:
        """Removes last-value rows for symbols leaving the scope.

        live_quotes is not self-trimming: left alone it only grows, and
        `yfin stream status` would show quotes for symbols nobody streams.
        """
        if not symbols:
            return 0
        with self._session_factory() as session:
            result = session.execute(
                text("DELETE FROM live_quotes WHERE symbol = ANY(:symbols)"),
                {"symbols": list(symbols)},
            )
            session.commit()
            # CursorResult, not Result: rowcount only exists on the DML
            # form and mypy cannot narrow it from execute()'s signature.
            return int(cast("CursorResult[Any]", result).rowcount or 0)
