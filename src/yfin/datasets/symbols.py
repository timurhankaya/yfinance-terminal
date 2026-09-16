"""symbols dataset - infrastructure, always runs first."""

from __future__ import annotations

from typing import Any

from yfin.core import normalize as nz
from yfin.datasets.base import Dataset, NormalizedResult, SyncContext
from yfin.datasets.common import convert_field
from yfin.datasets.payloads import SymbolsPayload
from yfin.datasets.registry import register
from yfin.ingest.client import call_yahoo
from yfin.models.fields import HISTORY_METADATA_FIELDS, Field
from yfin.storage.contracts import TableWrite

# fast_info and history_metadata share the same two calls; ctx.cached
# prevents a repeat request.
CACHE_FAST_INFO = "fast_info"
CACHE_HISTORY_METADATA = "history_metadata"


def fetch_fast_info(ctx: SyncContext) -> Any:
    return ctx.cached(
        CACHE_FAST_INFO,
        lambda: call_yahoo(ctx.ticker.get_fast_info, what=f"fast_info:{ctx.symbol}"),
    )


def fetch_history_metadata(ctx: SyncContext) -> Any:
    return ctx.cached(
        CACHE_HISTORY_METADATA,
        lambda: call_yahoo(ctx.ticker.get_history_metadata, what=f"history_metadata:{ctx.symbol}"),
    )


_HM: dict[str, Field] = {f.source: f for f in HISTORY_METADATA_FIELDS}


def _upper(value: str | None) -> str | None:
    """Passes None through; otherwise uppercases.

    A symbol added via `yfin symbols add` has `exchange`/`quote_type` NULL
    UNTIL THE FIRST SYNC; must not raise on None.
    """
    return value.upper() if value is not None else None


class SymbolsDataset(Dataset[SymbolsPayload]):
    name = "symbols"
    depends_on = ()
    produces = ("symbols",)

    def fetch(self, ctx: SyncContext) -> SymbolsPayload:
        return SymbolsPayload(
            fast_info=fetch_fast_info(ctx),
            metadata=fetch_history_metadata(ctx),
            fetched_at=ctx.fetched_at,
        )

    def normalize(self, raw: SymbolsPayload, symbol: str) -> NormalizedResult:
        fast_info = raw.fast_info
        metadata = raw.metadata or {}
        fi = nz.as_mapping(fast_info) if fast_info is not None else {}
        md = nz.as_mapping(metadata)

        row: dict[str, Any] = {
            "symbol": symbol,
            # Columns are COLLATE "C" (case sensitive); this is the single
            # place case normalization happens for these two fields.
            "quote_type": _upper(nz.to_str(fi.get("quoteType") or md.get("instrumentType"), 32)),
            "exchange": _upper(nz.to_str(fi.get("exchange") or md.get("exchangeName"), 32)),
            "full_exchange_name": nz.to_str(md.get("fullExchangeName"), 64),
            "currency": nz.to_str(fi.get("currency") or md.get("currency"), 32),
            "timezone": nz.to_str(fi.get("timezone") or md.get("timezone"), 64),
            "short_name": nz.to_str(md.get("shortName"), 128),
            "long_name": nz.to_str(md.get("longName"), 255),
            "first_trade_date": convert_field(_HM["firstTradeDate"], md.get("firstTradeDate")),
            "is_active": True,
            "unknown_streak": 0,
            "last_seen_at": raw.fetched_at,
        }

        # `is_active` is in the row so the first INSERT defaults to 1, but
        # out of the update scope so a sync never reactivates a manually
        # deactivated symbol.
        frozen = {"symbol", "is_active"}
        update_columns = tuple(c for c in row if c not in frozen)

        return NormalizedResult(
            writes=[
                TableWrite(
                    table="symbols",
                    rows=[row],
                    key_columns=("symbol",),
                    update_columns=update_columns,
                )
            ]
        )


register(SymbolsDataset())
