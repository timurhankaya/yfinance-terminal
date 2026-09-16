"""Hard-deleting a symbol, and publishing the deletions.

The order comes from `symbol_scoped_tables()`, derived from the FK edges:
`ON DELETE RESTRICT` enforces the soft-delete policy, so children go first.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Table, delete
from sqlalchemy.orm import Session

from yfin.core.families import DataFamily
from yfin.models import Base, symbol_scoped_tables
from yfin.storage.changes import (
    BARS_INTERVAL_TABLES,
    BARS_TIME_COLUMN,
    ChangeCollector,
)
from yfin.storage.db import rowcount
from yfin.storage.routing import INFRASTRUCTURE_TABLES, ROUTES


def purge_symbol(
    session: Session, symbol: str, *, collector: ChangeCollector | None = None
) -> dict[str, int]:
    """Deletes every row a symbol owns. Returns the count per table.

    The caller commits. Deletions publish row-level, except bars tables,
    which publish one `range` event with `kind: delete`.
    """
    removed: dict[str, int] = {}
    for name in symbol_scoped_tables():
        count = _purge_table(session, Base.metadata.tables[name], symbol, collector)
        if count:
            removed[name] = count

    # `news_symbols` and `symbols` are not in `symbol_scoped_tables()`: the
    # first is keyed by (news_id, symbol) and the second IS the parent.
    link = _purge_table(session, Base.metadata.tables["news_symbols"], symbol, collector)
    if link:
        removed["news_symbols"] = link
    root = _purge_table(session, Base.metadata.tables["symbols"], symbol, collector)
    if root:
        removed["symbols"] = root
    return removed


def delete_rows(
    session: Session,
    table: Table,
    where: Any,
    *,
    collector: ChangeCollector | None = None,
) -> int:
    """One DELETE, published as one `delete` event per removed row.

    Bars-family tables are refused: returning their keys is the cost a
    range event exists to avoid, so `purge_symbol` routes them to `_purge_bars`.
    """
    if collector is None or table.name in INFRASTRUCTURE_TABLES:
        return rowcount(session.execute(delete(table).where(where)))

    if ROUTES[table.name].family is DataFamily.BARS:
        raise ValueError(
            f"{table.name} is a bars table; returning its keys from a DELETE is what "
            "range events exist to avoid. Use purge_symbol, which emits one span."
        )

    key_columns = [c.name for c in table.primary_key]
    deleted = session.execute(
        delete(table).where(where).returning(*(table.c[name] for name in key_columns))
    )
    count = 0
    for row in deleted.mappings():
        collector.record(table.name, "delete", dict(row), None)
        count += 1
    return count


def _purge_table(
    session: Session, table: Table, symbol: str, collector: ChangeCollector | None
) -> int:
    """One table's rows for one symbol, with the right shape of event."""
    where = table.c["symbol"] == symbol
    if (
        collector is not None
        and table.name not in INFRASTRUCTURE_TABLES
        and ROUTES[table.name].family is DataFamily.BARS
    ):
        return _purge_bars(session, table, symbol, collector)
    return delete_rows(session, table, where, collector=collector)


def _purge_bars(
    session: Session, table: Table, symbol: str, collector: ChangeCollector
) -> int:
    """A bars table: the span goes, the keys do not come back.

    One event per (symbol, interval) where the table has one. The span is
    left null: reading min and max first would cost an extra scan.
    """
    where = table.c["symbol"] == symbol
    ts_column = BARS_TIME_COLUMN[table.name]

    if table.name in BARS_INTERVAL_TABLES:
        result = session.execute(
            delete(table).where(where).returning(table.c["bar_interval"])
        )
        per_interval: dict[str, int] = {}
        for (interval,) in result:
            per_interval[interval] = per_interval.get(interval, 0) + 1
        for interval, count in per_interval.items():
            collector.record_range(
                table.name,
                symbol,
                kind="delete",
                bar_interval=interval,
                ts_column=ts_column,
                ts_from=None,
                ts_to=None,
                rows=count,
            )
        return sum(per_interval.values())

    result = session.execute(delete(table).where(where))
    count = rowcount(result)
    if count:
        collector.record_range(
            table.name,
            symbol,
            kind="delete",
            bar_interval=None,
            ts_column=ts_column,
            ts_from=None,
            ts_to=None,
            rows=count,
        )
    return count


__all__ = ["delete_rows", "purge_symbol"]
