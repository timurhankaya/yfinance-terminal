"""The read layer. Every SQL statement in the API lives here.

Keeping it in one layer is what makes the rest of the rules enforceable:
routers cannot quietly grow a query, the cursor keys and the sort orders
sit next to each other, and a later move to async touches this file
rather than every endpoint.

Two shapes recur and are worth stating once.

Rows come back as plain dicts, not ORM objects. The three bar tables have
different columns -- `price_history` is keyed on the exchange session
date and carries adj_close; `price_bars` carries bar_interval, local_date
and is_extended; `periodic_bars` has no is_extended because "outside
regular hours" is meaningless above daily -- and a dict lets one response
schema describe all three with the absent fields as null.

Every list query asks for one row more than the page size. If that row
comes back there is a next page, and its key becomes the cursor; that is
one query per page instead of a second count query.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Row, Select, and_, func, or_, select, text
from sqlalchemy.orm import Session

from yfin.api.storage import limits
from yfin.models import bars_table_for
from yfin.models.bars import PeriodicBar, PriceBar
from yfin.models.financials import FinancialFact, FinancialPeriod
from yfin.models.prices import PriceHistory
from yfin.models.snapshots import ticker_info
from yfin.models.symbols import Symbol

#: Only regular-session bars unless the caller asks otherwise. This is
#: accident prevention, not convenience: mixing thin extended-hours bars
#: into a regular series quietly corrupts every indicator computed from
#: it, and the mistake is invisible in the output.
SESSION_REGULAR = "regular"
SESSION_ALL = "all"


@dataclass(frozen=True)
class Page:
    rows: list[dict[str, Any]]
    next_key: tuple[Any, ...] | None


def _page(rows: Sequence[Row[Any]], limit: int, key: Any) -> tuple[list[Any], Any]:
    has_more = len(rows) > limit
    visible = rows[:limit]
    return list(visible), (key(visible[-1]) if has_more and visible else None)


# --- symbols ----------------------------------------------------------------

SYMBOL_COLUMNS = (
    Symbol.symbol,
    Symbol.isin,
    Symbol.quote_type,
    Symbol.exchange,
    Symbol.full_exchange_name,
    Symbol.currency,
    Symbol.timezone,
    Symbol.short_name,
    Symbol.long_name,
    Symbol.first_trade_date,
    Symbol.is_active,
)


def list_symbols(
    session: Session,
    *,
    exchange: str | None,
    quote_type: str | None,
    prefix: str | None,
    active: bool,
    limit: int,
    after: tuple[Any, ...] | None,
) -> Page:
    statement: Select[Any] = select(*SYMBOL_COLUMNS).where(Symbol.is_active == active)
    if exchange:
        statement = statement.where(Symbol.exchange == exchange.upper())
    if quote_type:
        statement = statement.where(Symbol.quote_type == quote_type.upper())
    if prefix:
        # Prefix only, and escaped: `symbol LIKE 'X%'` uses the primary
        # key, while a leading wildcard would scan the table.
        pattern = limits.escape_prefix(prefix.upper()) + "%"
        statement = statement.where(Symbol.symbol.like(pattern, escape="\\"))
    if after is not None:
        statement = statement.where(Symbol.symbol > after[0])

    rows = session.execute(statement.order_by(Symbol.symbol).limit(limit + 1)).all()
    visible, next_key = _page(rows, limit, lambda row: (row.symbol,))
    return Page(rows=[dict(row._mapping) for row in visible], next_key=next_key)


def get_symbol(session: Session, symbol: str) -> dict[str, Any] | None:
    row = session.execute(
        select(*SYMBOL_COLUMNS).where(Symbol.symbol == symbol)
    ).one_or_none()
    if row is None:
        return None
    detail = dict(row._mapping)
    detail["info"] = _latest_info(session, symbol)
    return detail


def _latest_info(session: Session, symbol: str) -> dict[str, Any] | None:
    """The identity snapshot, if one has been fetched.

    A separate query rather than a join: `ticker_info` is wide, and a
    symbol that has never synced simply has no row -- which is a normal
    state, not an error.
    """
    row = session.execute(
        select(ticker_info).where(ticker_info.c.symbol == symbol)
    ).one_or_none()
    if row is None:
        return None
    mapping = dict(row._mapping)
    # raw_json is the fetch artifact, not part of the contract; content_hash
    # is an internal gate detail.
    for internal in ("raw_json", "content_hash"):
        mapping.pop(internal, None)
    return mapping


def symbol_exists(session: Session, symbol: str) -> bool:
    return (
        session.execute(select(Symbol.symbol).where(Symbol.symbol == symbol)).first()
        is not None
    )


# --- bars -------------------------------------------------------------------


def _bar_selection(interval: str) -> tuple[Any, tuple[Any, ...], Any]:
    """(model, columns, time column) for an interval.

    The table comes from `bars_table_for`, which is the single source the
    write path uses too; the API does not keep a second mapping that could
    drift from it.
    """
    table = bars_table_for(interval)
    if table == "price_history":
        return (
            PriceHistory,
            (
                PriceHistory.symbol,
                PriceHistory.ts_utc,
                PriceHistory.session_date,
                PriceHistory.open,
                PriceHistory.high,
                PriceHistory.low,
                PriceHistory.close,
                PriceHistory.adj_close,
                PriceHistory.volume,
            ),
            PriceHistory.ts_utc,
        )
    model = PriceBar if table == "price_bars" else PeriodicBar
    columns = [
        model.symbol,
        model.bar_interval,
        model.ts_utc,
        model.local_date,
        model.open,
        model.high,
        model.low,
        model.close,
        model.volume,
    ]
    if table == "price_bars":
        columns.append(PriceBar.is_extended)
    return model, tuple(columns), model.ts_utc


def list_bars(
    session: Session,
    *,
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
    session_kind: str,
    limit: int,
    after: tuple[Any, ...] | None,
) -> Page:
    model, columns, time_column = _bar_selection(interval)
    statement: Select[Any] = select(*columns).where(
        model.symbol == symbol,
        time_column >= start,
        # Half-open: `to` is exclusive, so consecutive pages do not
        # overlap by a row at the boundary.
        time_column < end,
    )

    if model is not PriceHistory:
        statement = statement.where(model.bar_interval == interval)
    if model is PriceBar and session_kind == SESSION_REGULAR:
        # The same filter `v_price_bars_regular` applies. Expressed here
        # rather than by selecting the view because the view exposes a
        # narrower column set than this endpoint returns.
        statement = statement.where(PriceBar.is_extended.is_(False))

    if after is not None:
        statement = statement.where(time_column > after[0])

    rows = session.execute(statement.order_by(time_column).limit(limit + 1)).all()
    visible, next_key = _page(rows, limit, lambda row: (row.ts_utc,))
    return Page(rows=[dict(row._mapping) for row in visible], next_key=next_key)


# --- corporate actions ------------------------------------------------------

#: `v_actions` unions dividends, splits and capital gains. A view has no
#: primary key, so the sort key is stated here: (date, type) is unique
#: per symbol because each source table is keyed on (symbol, date).
_ACTIONS = text(
    """
    SELECT symbol, action_date, action_type, action_value
      FROM v_actions
     WHERE symbol = :symbol
       AND action_date >= :start
       AND action_date < :end
       -- Casts are required, not cosmetic: PostgreSQL cannot infer a
       -- parameter's type from `$n IS NULL` alone and refuses the
       -- statement with "could not determine data type".
       AND (CAST(:after_date AS date) IS NULL
            OR (action_date, action_type)
               > (CAST(:after_date AS date), CAST(:after_type AS varchar)))
     ORDER BY action_date, action_type
     LIMIT :limit
    """
)


def list_actions(
    session: Session,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    limit: int,
    after: tuple[Any, ...] | None,
) -> Page:
    rows = session.execute(
        _ACTIONS,
        {
            "symbol": symbol,
            "start": start.date(),
            "end": end.date(),
            "after_date": after[0] if after else None,
            "after_type": after[1] if after else None,
            "limit": limit + 1,
        },
    ).all()
    visible, next_key = _page(
        rows, limit, lambda row: (row.action_date, row.action_type)
    )
    return Page(rows=[dict(row._mapping) for row in visible], next_key=next_key)


# --- financials -------------------------------------------------------------


def list_financials(
    session: Session,
    *,
    symbol: str,
    statement_kind: str,
    freq: str,
    limit: int,
    after: tuple[Any, ...] | None,
) -> Page:
    statement: Select[Any] = select(
        FinancialFact.period_end,
        FinancialFact.item_key,
        FinancialFact.value,
        FinancialPeriod.currency,
    ).join(
        FinancialPeriod,
        and_(
            FinancialFact.symbol == FinancialPeriod.symbol,
            FinancialFact.statement == FinancialPeriod.statement,
            FinancialFact.freq == FinancialPeriod.freq,
            FinancialFact.period_end == FinancialPeriod.period_end,
        ),
    ).where(
        FinancialFact.symbol == symbol,
        FinancialFact.statement == statement_kind,
        FinancialFact.freq == freq,
    )

    if after is not None:
        # Newest period first, so the keyset walks downwards.
        statement = statement.where(
            or_(
                FinancialFact.period_end < after[0],
                and_(
                    FinancialFact.period_end == after[0],
                    FinancialFact.item_key > after[1],
                ),
            )
        )

    rows = session.execute(
        statement.order_by(
            FinancialFact.period_end.desc(), FinancialFact.item_key
        ).limit(limit + 1)
    ).all()
    visible, next_key = _page(rows, limit, lambda row: (row.period_end, row.item_key))
    return Page(rows=[dict(row._mapping) for row in visible], next_key=next_key)


# --- freshness --------------------------------------------------------------


def financials_as_of(
    session: Session, symbol: str, statement_kind: str, freq: str
) -> datetime | None:
    """When these statements were last verified against the source."""
    return session.execute(
        select(func.max(FinancialPeriod.fetched_at)).where(
            FinancialPeriod.symbol == symbol,
            FinancialPeriod.statement == statement_kind,
            FinancialPeriod.freq == freq,
        )
    ).scalar_one_or_none()


