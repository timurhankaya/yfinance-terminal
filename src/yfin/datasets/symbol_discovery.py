"""Shared helpers for the datasets that DISCOVER symbols.

`search`, `lookup` and `screener` write `symbols` rows nobody asked for;
this is what they share.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

from yfin.core.logging_setup import get_logger

log = get_logger(__name__)


def symbol_is_writable(symbol: str) -> bool:
    """Whether the symbol can be written to the `symbols` table.

    Only the column length is checked; narrowing the character set would
    reject indices (`^...`).
    """
    from yfin.models.base import SYMBOL_LENGTH

    return len(symbol) <= SYMBOL_LENGTH and symbol.isascii()


def utc_as_of_day(fetched_at: datetime) -> date:
    """Derives `as_of_date` from the fetch timestamp, not from `now()`.

    Otherwise `as_of_date` and `fetched_at` diverge across midnight.
    Discovery is region-independent, so UTC.
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

    `is_active`, `discovered_by` and `discovered_at` take effect only on INSERT
    (excluded from every `update_columns`), so manual activation survives.
    """
    return {
        "symbol": symbol,
        **typed_fields,
        "is_active": False,
        "discovered_by": source,
        "discovered_at": fetched_at,
        "last_seen_at": fetched_at,
    }
