"""Read-only providers the worker threads call.

`SyncContext` carries no database handle; each provider opens its own
short session behind a lock, so the main transaction is never touched.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session, sessionmaker

from yfin.core import metrics
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
    """Resolves intraday_scope, reading each interval once per run.

    A fresh SyncContext is built per symbol, so the cache cannot live there.
    """

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory
        self._lock = threading.Lock()
        self._cache: dict[str, frozenset[str] | None] = {}

    def _symbols_for(self, interval: str) -> frozenset[str] | None:
        """Scope set for an interval; None = the full universe.

        "Has rows" is independent of `enabled`: an interval with only
        enabled=0 rows runs no symbols.
        """
        if interval in self._cache:
            metrics.inc("yfin_sync_cache_ops_total", cache="scope_reader", result="hit")
            return self._cache[interval]
        metrics.inc("yfin_sync_cache_ops_total", cache="scope_reader", result="miss")
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

    Once the watermark moves past a failed slice, only this brings it back.
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
