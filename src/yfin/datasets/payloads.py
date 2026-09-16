"""Typed contracts between fetch and normalize.

`Dataset[RawT]` makes fetch's return type and normalize's input match
under `mypy --strict`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import pandas as pd


@dataclass(frozen=True, slots=True)
class SymbolsPayload:
    """fast_info + history_metadata; both come through ctx.cached."""

    fast_info: Any
    metadata: Any
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class MetadataPayload:
    metadata: Any
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class InfoPayload:
    info: Mapping[str, Any] | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class FastInfoPayload:
    fast_info: Any
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class StatementPayload:
    """Financial statement: index=item label, columns=period end.

    A symbol with no company returns an empty (0,0) DataFrame, not an
    exception. currency is info.financialCurrency and is BEST-EFFORT.
    """

    frame: pd.DataFrame | None
    currency: str | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class CalendarPayload:
    """get_calendar(): a 9-key dict; keys go missing depending on symbol."""

    calendar: Mapping[str, Any] | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class EarningsDatesPayload:
    """Paginated earnings dates; each page is fetched with a FRESH Ticker."""

    frame: pd.DataFrame | None
    fetched_at: datetime
    #: Whether paging reached an empty page rather than the page cap. Only
    #: a complete history may replace what is stored -- truncating and then
    #: replacing would delete the older rows the cap cut off.
    complete: bool = True


@dataclass(frozen=True, slots=True)
class SecFilingsPayload:
    """Outside the US, the source returns {} (dict), not a list."""

    filings: list[dict[str, Any]]
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class MarketStatusPayload:
    """Market(region).status; returns None for 7 regions other than US."""

    region: str
    status: Mapping[str, Any] | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class MarketSummaryPayload:
    """Market(region).summary; board code -> quote dict."""

    region: str
    summary: Mapping[str, Any] | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class CalendarFramePayload:
    """Paginated calendar frame; None once exhausted."""

    frame: pd.DataFrame | None
    fetched_at: datetime


# Single-column, date-indexed series; can also be None.
SeriesPayload = pd.Series | None
FramePayload = pd.DataFrame
NewsPayload = list[dict[str, Any]]


# --- analysis / ownership / funds -----------------------------------------


@dataclass(frozen=True, slots=True)
class AsOfFramePayload:
    """A frame carrying no date; `as_of_date` derives from `fetched_at`.

    Carried in the payload because `normalize` does not see ctx.
    """

    frame: pd.DataFrame | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class AsOfMappingPayload:
    """get_analyst_price_targets(): a 5-key dict."""

    payload: Mapping[str, Any] | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class FundsPayload:
    """get_funds_data(); `data=None` for a non-fund symbol (NO request made)."""

    data: Any | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class RangedFramePayload:
    """A frame that CARRIES its own source date + a `--start/--end` range.

    The range only narrows a fixed source window; it is carried here
    because `date_range="filter"` is applied in `normalize`, without ctx.
    """

    frame: pd.DataFrame | None
    fetched_at: datetime
    start: date | None = None
    end: date | None = None
