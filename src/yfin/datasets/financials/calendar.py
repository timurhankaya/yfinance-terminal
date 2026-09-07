"""ticker calendar dataset.

Source is a nine-key dict (`quote.py:_fetch_calendar` writes exactly these
nine, no more possible); keys go missing per symbol: MSFT lacks
Ex-Dividend Date, THYAO lacks Dividend Date, TSLA/BRK-B lack both. All
columns are therefore nullable.
"""

from __future__ import annotations

from typing import Any

from yfin.core import normalize as nz
from yfin.core.families import DataFamily
from yfin.core.logging_setup import get_logger
from yfin.datasets.base import NormalizedResult, SyncContext
from yfin.datasets.exposure import ApiExposure
from yfin.datasets.payloads import CalendarPayload
from yfin.datasets.registry import register
from yfin.datasets.snapshot_base import SnapshotDataset
from yfin.ingest.client import call_optional
from yfin.storage.contracts import TableWrite

log = get_logger(__name__)

DATA_COLUMNS = (
    "dividend_date",
    "ex_dividend_date",
    "earnings_date_start",
    "earnings_date_end",
    "earnings_date_count",
    "earnings_high",
    "earnings_low",
    "earnings_average",
    "revenue_high",
    "revenue_low",
    "revenue_average",
    "raw_json",
    "content_hash",
    "fetched_at",
)


class CalendarDataset(SnapshotDataset[CalendarPayload]):
    name = "calendar"
    depends_on = ("symbols",)
    produces = ("ticker_calendar", "ticker_calendar_history")
    snapshot_table = "ticker_calendar"
    history_table = "ticker_calendar_history"
    key_columns = ("symbol",)
    api = (
        ApiExposure(
            name="ticker_calendar",
            family=DataFamily.FUNDAMENTALS,
            table="ticker_calendar",
            sort_key=("symbol",),
            description="Next dividend and earnings dates for one symbol.",
        ),
        ApiExposure(
            name="ticker_calendar_history",
            family=DataFamily.FUNDAMENTALS,
            table="ticker_calendar_history",
            sort_key=("fetched_at",),
            descending=True,
            description="Point-in-time history of those dates.",
        ),
    )

    def fetch(self, ctx: SyncContext) -> CalendarPayload:
        # A symbol with no company returns 404 -> empty.
        calendar = ctx.cached(
            "calendar",
            lambda: call_optional(ctx.ticker.get_calendar, what=f"calendar:{ctx.symbol}"),
        )
        return CalendarPayload(calendar=calendar, fetched_at=ctx.fetched_at)

    def normalize(self, raw: CalendarPayload, symbol: str) -> NormalizedResult:
        calendar = raw.calendar
        if nz.is_empty_result(calendar):
            return NormalizedResult()
        payload = nz.as_mapping(calendar)

        dates = payload.get("Earnings Date") or []
        if not isinstance(dates, list | tuple):
            dates = [dates]
        if len(dates) > 2:
            # Source has no documented list-length limit; measured len=1 across
            # 6 symbols. Middle entries are dropped, hence the warning.
            log.warning("earnings date list has extra entries", symbol=symbol, count=len(dates))

        row: dict[str, Any] = {
            "symbol": symbol,
            "dividend_date": nz.to_local_date(payload.get("Dividend Date")),
            "ex_dividend_date": nz.to_local_date(payload.get("Ex-Dividend Date")),
            "earnings_date_start": nz.to_local_date(dates[0]) if dates else None,
            "earnings_date_end": nz.to_local_date(dates[-1]) if dates else None,
            "earnings_date_count": len(dates),
            "earnings_high": nz.to_decimal(payload.get("Earnings High")),
            "earnings_low": nz.to_decimal(payload.get("Earnings Low")),
            "earnings_average": nz.to_decimal(payload.get("Earnings Average")),
            "revenue_high": nz.to_decimal(payload.get("Revenue High")),
            "revenue_low": nz.to_decimal(payload.get("Revenue Low")),
            "revenue_average": nz.to_decimal(payload.get("Revenue Average")),
        }
        canonical = nz.canonical_json(dict(payload))
        row["raw_json"] = canonical
        row["content_hash"] = nz.content_hash(canonical=canonical)
        row["fetched_at"] = raw.fetched_at

        return NormalizedResult(
            writes=[
                TableWrite(
                    table="ticker_calendar",
                    rows=[dict(row)],
                    key_columns=("symbol",),
                    update_columns=DATA_COLUMNS,
                ),
                TableWrite(
                    table="ticker_calendar_history",
                    rows=[dict(row)],
                    key_columns=("symbol", "fetched_at"),
                    update_columns=DATA_COLUMNS,
                ),
            ]
        )


register(CalendarDataset())
