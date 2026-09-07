"""Shared helpers for the three datasets that DISCOVER symbols.

`search`, `lookup` and `screener` are the only datasets that write rows
into `symbols` for symbols nobody asked for. They each did the same four
things -- filter what is writable, pin an as-of day, unwrap the response,
build the `symbols` row -- and each had its own copy.

Kept out of `common.py`: that module is about turning a field map into
column values, which changes for entirely different reasons.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

from yfin.core.logging_setup import get_logger

log = get_logger(__name__)


def symbol_is_writable(symbol: str) -> bool:
    """Whether the symbol can be written to the `symbols` table.

    The constraint is derived from `SymbolType()` = VARCHAR(SYMBOL_LENGTH)
    COLLATE "C"; the length is not hardcoded here.

    `^` is in scope: 93 of 9,243 symbols measured start with it (indices).
    A validation that narrows the character set would reject indices wholesale.

    Not derived from write order -- computed inside `normalize` instead: an
    order-dependent derivation would silently break if the gate's scope changed.
    """
    from yfin.models.base import SYMBOL_LENGTH

    return len(symbol) <= SYMBOL_LENGTH and symbol.isascii()


def utc_as_of_day(fetched_at: datetime) -> date:
    """Derives `as_of_date` from the fetch timestamp, not from `now()`.

    Three datasets used to call `datetime.now(UTC).date()`; that made the
    gate row's `as_of_date` and `fetched_at` come from different time
    sources and diverge across midnight -- rows could get a September 5
    timestamp but land on the September 6 day. `domain/common.as_of_day`
    applies the same principle for the market timezone; discovery is
    region-independent, so it uses UTC.
    """
    moment = fetched_at if fetched_at.tzinfo is not None else fetched_at.replace(tzinfo=UTC)
    return moment.astimezone(UTC).date()


def expect_dict(value: Any, *, what: str) -> dict[str, Any]:
    """Raises loudly if the response is not a dict.

    Returning empty silently would conflate "no data" (`empty`) with "the
    response shape changed" (`failed`); the latter needs to be seen right away.
    """
    if not isinstance(value, dict):
        raise TypeError(f"the {what} response is not a dict: {type(value).__name__}")
    return value


def dict_items(payload: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    """Dict items in the `payload[key]` list; other items are filtered out.

    If a source block returns an unexpected scalar, only that one row
    should drop, not the whole cell.
    """
    return [item for item in payload.get(key) or [] if isinstance(item, dict)]


def discovered_symbol_row(
    symbol: str,
    *,
    source: str,
    fetched_at: datetime,
    **typed_fields: Any,
) -> dict[str, Any]:
    """`symbols` row for a discovered symbol.

    The four common fields live here, in one place. Source-specific
    identifying fields pass through `typed_fields` -- each path supplies
    only what it actually populates, and `update_columns` is kept narrow
    to match.

    `is_active`, `discovered_by`, and `discovered_at` take effect only on
    INSERT: all three paths' `update_columns` exclude them. If they were in
    scope, a symbol an operator manually activated would silently flip back
    to inactive the next time it was rediscovered.
    """
    return {
        "symbol": symbol,
        **typed_fields,
        "is_active": False,
        "discovered_by": source,
        "discovered_at": fetched_at,
        "last_seen_at": fetched_at,
    }
