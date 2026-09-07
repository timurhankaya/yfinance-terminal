"""Read-only providers the worker threads call.

All three exist for the same reason: `SyncContext` carries no database
handle, and a fresh context is built per symbol. Each opens its own short
session behind a lock, so a worker thread can ask a question without
touching the main transaction or holding it open.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session, sessionmaker

from yfin.models import GAP_FETCH_FAILED, Base


class WatermarkReader:
    """Read-only watermark provider. Opens its own short session so worker
    threads never touch the main transaction."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory
        self._lock = threading.Lock()

    def __call__(
        self,
        table: str,
        column: str,
        symbol: str,
        *,
        where: Mapping[str, Any] | None = None,
    ) -> date | datetime | None:
        target = Base.metadata.tables[table]
        conditions = [target.c["symbol"] == symbol]
        # price_bars needs a separate watermark per interval.
        for name, value in (where or {}).items():
            conditions.append(target.c[name] == value)
        stmt = select(func.max(target.c[column])).where(and_(*conditions))
        with self._lock, self._factory() as session:
            result = session.execute(stmt).scalar_one_or_none()
        return result


class ScopeReader:
    """Resolves intraday_scope.

    SyncContext has no DB access and `_worker` builds a fresh SyncContext
    per symbol, so caching the scope query on ctx would mean ~5,000
    queries per run. This instance reads it once per run instead; each
    shard child process gets its own instance.
    """

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory
        self._lock = threading.Lock()
        self._cache: dict[str, frozenset[str] | None] = {}

    def _symbols_for(self, interval: str) -> frozenset[str] | None:
        """Scope set for an interval; None = the full universe.

        Applied per bar_interval, independent of `enabled`: the rule is
        "does at least one row exist for this interval?". An interval
        with only enabled=0 rows still counts as "has rows" and runs no
        symbols.
        """
        if interval in self._cache:
            return self._cache[interval]
        table = Base.metadata.tables["intraday_scope"]
        with self._lock, self._factory() as session:
            any_row = session.execute(
                select(func.count()).select_from(table).where(table.c["bar_interval"] == interval)
            ).scalar_one()
            value: frozenset[str] | None
            if not any_row:
                # No rows for 1m means no symbols (risk: 1.21B rows/year);
                # for other intervals it means the full universe.
                value = frozenset() if interval == "1m" else None
            else:
                rows = session.execute(
                    select(table.c["symbol"]).where(
                        table.c["bar_interval"] == interval, table.c["enabled"].is_(True)
                    )
                ).scalars()
                value = frozenset(rows)
        self._cache[interval] = value
        return value

    def __call__(self, symbol: str, interval: str) -> bool:
        allowed = self._symbols_for(interval)
        return True if allowed is None else symbol in allowed


class GapReader:
    """Reads open (unresolved) gaps.

    Without this feedback loop bar_gaps would just be a tombstone: if a
    middle slice fails and later slices succeed, the watermark moves past
    the gap and that window is never requested again.
    """

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory
        self._lock = threading.Lock()

    def __call__(self, symbol: str, interval: str) -> list[tuple[datetime, datetime]]:
        table = Base.metadata.tables["bar_gaps"]
        stmt = select(table.c["gap_start_utc"], table.c["gap_end_utc"]).where(
            table.c["symbol"] == symbol,
            table.c["bar_interval"] == interval,
            table.c["reason"] == GAP_FETCH_FAILED,
            table.c["resolved_at"].is_(None),
        )
        with self._lock, self._factory() as session:
            return [(row[0], row[1]) for row in session.execute(stmt)]
