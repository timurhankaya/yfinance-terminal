"""Calendar datasets: earnings, economic, IPO, splits.

`filter_most_active=False` is explicit: the default applies only to page 0 and
paging would skip rows. An empty page has raw column names: check emptiness first.
"""

from __future__ import annotations

from abc import abstractmethod
from datetime import datetime
from functools import partial
from typing import Any

import pandas as pd

from yfin.core import normalize as nz
from yfin.core.config import get_settings
from yfin.core.families import DataFamily
from yfin.core.logging_setup import get_logger
from yfin.datasets.base import NormalizedResult, mark_known_in, plain_upsert
from yfin.datasets.common import key_value
from yfin.datasets.exposure import ApiExposure
from yfin.datasets.market.base import GlobalDataset, MarketContext
from yfin.datasets.payloads import CalendarFramePayload
from yfin.datasets.registry import register_market
from yfin.ingest.client import call_yahoo
from yfin.storage.contracts import RowWriter, TableWrite, WriteStats

log = get_logger(__name__)


def _calendars(mctx: MarketContext) -> Any:
    from yfinance import Calendars

    return mctx.cached("calendars", lambda: Calendars())


def _fetch_pages(mctx: MarketContext, method: str, **extra: Any) -> pd.DataFrame | None:
    cfg = get_settings()
    calendars = _calendars(mctx)
    frames: list[pd.DataFrame] = []
    complete = False
    for page in range(cfg.yf_calendar_max_pages):
        offset = page * cfg.yf_calendar_page_limit
        frame = call_yahoo(
            partial(
                getattr(calendars, method),
                start=mctx.start,
                end=mctx.end,
                limit=cfg.yf_calendar_page_limit,
                offset=offset,
                **extra,
            ),
            what=f"{method}:{offset}",
        )
        if nz.is_empty_result(frame):
            complete = True
            break
        frames.append(frame)
    if not complete:
        # The page cap is reachable: an earnings season carries more events
        # than the page budget, and everything past it is dropped.
        log.warning(
            "calendar hit the page cap; events beyond it were not fetched",
            method=method,
            max_pages=cfg.yf_calendar_max_pages,
            page_limit=cfg.yf_calendar_page_limit,
            start=str(mctx.start),
            end=str(mctx.end),
        )
    if not frames:
        return None
    return pd.concat(frames)


class CalendarDatasetBase(GlobalDataset[CalendarFramePayload]):
    """Common: pagination, PK deduplication, and is_known marking."""

    table: str
    key_columns: tuple[str, ...]
    update_columns: tuple[str, ...]
    method: str
    extra_args: dict[str, Any] = {}  # noqa: RUF012
    has_symbol = True

    def fetch(self, mctx: MarketContext) -> CalendarFramePayload:
        frame = _fetch_pages(mctx, self.method, **self.extra_args)
        return CalendarFramePayload(frame=frame, fetched_at=mctx.fetched_at)

    @abstractmethod
    def build_row(self, index: Any, record: Any, fetched_at: datetime) -> dict[str, Any] | None:
        """Converts one source row into a dict to write to the table.

        Returning None SKIPS the row (a PK component is missing/NaT);
        abstract because every calendar endpoint has a different column set.
        """

    def normalize(self, raw: CalendarFramePayload) -> NormalizedResult:
        frame = raw.frame
        if nz.is_empty_result(frame):
            return NormalizedResult()
        assert isinstance(frame, pd.DataFrame)

        rows: dict[tuple[Any, ...], dict[str, Any]] = {}
        for index, record in frame.iterrows():
            row = self.build_row(index, record, raw.fetched_at)
            if row is None:
                continue
            # Deduplicated by PK after pages are concatenated; otherwise
            # rows_verified < rows_attempted would wrongly produce `failed`.
            rows[tuple(row[c] for c in self.key_columns)] = row

        if not rows:
            return NormalizedResult()
        return NormalizedResult(
            writes=[
                TableWrite(
                    table=self.table,
                    rows=list(rows.values()),
                    key_columns=self.key_columns,
                    update_columns=self.update_columns,
                )
            ]
        )

    def upsert(
        self, writer: RowWriter, result: NormalizedResult, *, full_refresh: bool = False
    ) -> WriteStats:
        """`full_refresh` is accepted and ignored: this dataset has no gate."""
        marked = mark_known_in(
            writer, result, select=lambda write: self.has_symbol and bool(write.rows)
        )
        return plain_upsert(writer, marked)


def _symbol_of(index: Any, *, dataset: str) -> str | None:
    """The symbol feeding the PK. NOT TRUNCATED (`common.key_value` contract)."""
    text = key_value(index, 32, field="symbol", dataset=dataset, symbol=str(index)[:32])
    return nz.normalize_symbol(text) if text else None


class EarningsCalendarDataset(CalendarDatasetBase):
    name = "earnings_calendar"
    produces = ("calendar_earnings",)
    table = "calendar_earnings"
    method = "get_earnings_calendar"
    # The True default applies only at offset==0 -> page 0 and pages 1+ come
    # from different universes, causing data loss.
    extra_args = {"filter_most_active": False}  # noqa: RUF012
    key_columns = ("symbol", "event_start_ts_utc")
    update_columns = (
        "company",
        "market_cap",
        "event_name",
        "timing",
        "eps_estimate",
        "reported_eps",
        "surprise_pct",
        "is_known",
        "fetched_at",
    )
    api = (
        ApiExposure(
            family=DataFamily.FUNDAMENTALS,
            table="calendar_earnings",
            sort_key=("event_start_ts_utc", "symbol"),
            descending=True,
            symbol_optional=True,
            description="Upcoming and past earnings events across the market.",
        ),
    )

    def build_row(self, index: Any, record: Any, fetched_at: datetime) -> dict[str, Any] | None:
        symbol = _symbol_of(index, dataset=self.name)
        ts = nz.to_datetime_utc(record.get("Event Start Date"))
        if symbol is None or ts is None:
            log.warning("calendar row missing key", table=self.table)
            return None
        return {
            "symbol": symbol,
            "event_start_ts_utc": ts,
            "company": nz.to_str(record.get("Company"), max_len=255),
            "market_cap": nz.to_decimal(record.get("Marketcap")),
            "event_name": nz.to_str(record.get("Event Name"), max_len=128),
            "timing": nz.to_str(record.get("Timing"), max_len=8),
            "eps_estimate": nz.to_decimal(record.get("EPS Estimate")),
            "reported_eps": nz.to_decimal(record.get("Reported EPS")),
            "surprise_pct": nz.to_decimal(record.get("Surprise(%)")),
            "is_known": False,
            "fetched_at": fetched_at,
        }


class EconomicCalendarDataset(CalendarDatasetBase):
    name = "economic_calendar"
    produces = ("calendar_economic",)
    table = "calendar_economic"
    method = "get_economic_events_calendar"
    has_symbol = False
    # Index (Event) is NOT unique (29 repeats in 100 rows); the triple key is.
    key_columns = ("region", "event_time_utc", "event_name")
    update_columns = ("period_for", "actual", "expected", "last_reported", "revised", "fetched_at")
    api = (
        ApiExposure(
            family=DataFamily.FUNDAMENTALS,
            table="calendar_economic",
            sort_key=("event_time_utc", "event_name"),
            descending=True,
            filters=("region",),
            description="Scheduled macroeconomic events by region.",
        ),
    )

    def build_row(self, index: Any, record: Any, fetched_at: datetime) -> dict[str, Any] | None:
        # Both are PK components -> NOT TRUNCATED (key_value contract): two
        # events sharing the first 64 characters would collapse into one row.
        event_name = key_value(
            index, 64, field="event_name", dataset=self.name, symbol=str(index)[:32]
        )
        region = key_value(
            record.get("Region"), 16, field="region", dataset=self.name, symbol="-"
        )
        ts = nz.to_datetime_utc(record.get("Event Time"))
        if event_name is None or region is None or ts is None:
            log.warning("calendar row missing key", table=self.table)
            return None
        return {
            "region": region,
            "event_time_utc": ts,
            "event_name": event_name,
            "period_for": nz.to_str(record.get("For"), max_len=16),
            "actual": nz.to_decimal(record.get("Actual")),
            "expected": nz.to_decimal(record.get("Expected")),
            # 'last_value' is a window-function name (in PostgreSQL too).
            "last_reported": nz.to_decimal(record.get("Last")),
            "revised": nz.to_decimal(record.get("Revised")),
            "fetched_at": fetched_at,
        }


class IpoCalendarDataset(CalendarDatasetBase):
    name = "ipo_calendar"
    produces = ("calendar_ipo",)
    table = "calendar_ipo"
    method = "get_ipo_info_calendar"
    key_columns = ("symbol", "ipo_date_utc", "action")
    update_columns = (
        "company",
        "exchange",
        "filing_date",
        "amended_date",
        "price_from",
        "price_to",
        "price",
        "currency",
        "shares",
        "is_known",
        "fetched_at",
    )
    api = (
        ApiExposure(
            family=DataFamily.FUNDAMENTALS,
            table="calendar_ipo",
            sort_key=("ipo_date_utc", "symbol"),
            descending=True,
            symbol_optional=True,
            filters=("action",),
            description="IPO pricings, filings and withdrawals.",
        ),
    )

    def build_row(self, index: Any, record: Any, fetched_at: datetime) -> dict[str, Any] | None:
        symbol = _symbol_of(index, dataset=self.name)
        ts = nz.to_datetime_utc(record.get("Date"))
        action = key_value(
            record.get("Action"), 16, field="action", dataset=self.name, symbol=symbol or "-"
        )
        if symbol is None or ts is None or action is None:
            log.warning("calendar row missing key", table=self.table)
            return None
        return {
            "symbol": symbol,
            "ipo_date_utc": ts,
            "action": action,
            "company": nz.to_str(record.get("Company"), max_len=255),
            "exchange": nz.to_str(record.get("Exchange"), max_len=32),
            # Filing/Amended Date is often NaT.
            "filing_date": nz.to_local_date(record.get("Filing Date")),
            "amended_date": nz.to_local_date(record.get("Amended Date")),
            "price_from": nz.to_decimal(record.get("Price From")),
            "price_to": nz.to_decimal(record.get("Price To")),
            "price": nz.to_decimal(record.get("Price")),
            "currency": nz.to_str(record.get("Currency"), max_len=8),
            "shares": nz.to_decimal(record.get("Shares")),
            "is_known": False,
            "fetched_at": fetched_at,
        }


class SplitsCalendarDataset(CalendarDatasetBase):
    name = "splits_calendar"
    produces = ("calendar_splits",)
    table = "calendar_splits"
    method = "get_splits_calendar"
    key_columns = ("symbol", "payable_on_utc")
    update_columns = (
        "company",
        "optionable",
        "old_share_worth",
        "share_worth",
        "ratio",
        "is_known",
        "fetched_at",
    )
    api = (
        ApiExposure(
            family=DataFamily.FUNDAMENTALS,
            table="calendar_splits",
            sort_key=("payable_on_utc", "symbol"),
            descending=True,
            symbol_optional=True,
            description="Announced stock splits and their payable dates.",
        ),
    )

    def build_row(self, index: Any, record: Any, fetched_at: datetime) -> dict[str, Any] | None:
        symbol = _symbol_of(index, dataset=self.name)
        ts = nz.to_datetime_utc(record.get("Payable On"))
        if symbol is None or ts is None:
            log.warning("calendar row missing key", table=self.table)
            return None
        old_worth = nz.to_int(record.get("Old Share Worth"))
        new_worth = nz.to_int(record.get("Share Worth"))
        ratio = None
        if old_worth and new_worth is not None:
            ratio = nz.to_decimal(new_worth / old_worth)
        return {
            "symbol": symbol,
            "payable_on_utc": ts,
            "company": nz.to_str(record.get("Company"), max_len=255),
            "optionable": nz.to_bool(record.get("Optionable")),
            "old_share_worth": old_worth,
            "share_worth": new_worth,
            "ratio": ratio,
            "is_known": False,
            "fetched_at": fetched_at,
        }


register_market(EarningsCalendarDataset())
register_market(EconomicCalendarDataset())
register_market(IpoCalendarDataset())
register_market(SplitsCalendarDataset())
