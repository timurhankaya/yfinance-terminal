"""ticker calendar dataset'i (S6.5).

Kaynak dokuz anahtarlik bir dict'tir (`quote.py:_fetch_calendar` tam olarak
bu dokuzunu yazar, daha fazlasi imkansiz); sembole gore anahtar EKSIK olur:
MSFT'te Ex-Dividend Date, THYAO'da Dividend Date, TSLA/BRK-B'de ikisi de yok.
Bu yuzden tum kolonlar NULL kabul eder.
"""

from __future__ import annotations

from typing import Any

from yfin import normalize as nz
from yfin.client import call_optional
from yfin.datasets.base import NormalizedResult, SyncContext, TableWrite
from yfin.datasets.payloads import CalendarPayload
from yfin.datasets.registry import register
from yfin.datasets.snapshot_base import SnapshotDataset
from yfin.logging_setup import get_logger

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

    def fetch(self, ctx: SyncContext) -> CalendarPayload:
        # Sirket olmayan sembolde 404 -> empty (S8.2)
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
            # Kaynakta liste uzunluguna sinir YOK; olculen 6 sembolde len=1.
            # Ara elemanlar kaybolur, bu yuzden uyarilir.
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
