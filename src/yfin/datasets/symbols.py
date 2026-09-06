"""symbols dataset'i (S6.3 #0) - altyapi, her calistirmada ilk kosar."""

from __future__ import annotations

from typing import Any

from yfin import normalize as nz
from yfin.client import call_yahoo
from yfin.datasets.base import Dataset, NormalizedResult, SyncContext, TableWrite
from yfin.datasets.common import convert_field
from yfin.datasets.payloads import SymbolsPayload
from yfin.datasets.registry import register
from yfin.models.fields import HISTORY_METADATA_FIELDS, Field

# fast_info + history_metadata ayni iki cagriyi paylasir; ctx.cached tekrar
# istegini onler (S6.3)
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
            "quote_type": nz.to_str(fi.get("quoteType") or md.get("instrumentType"), 32),
            "exchange": nz.to_str(fi.get("exchange") or md.get("exchangeName"), 32),
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

        # Kolon sahipligi dataset basina tekildir (S6.1/3). Iki kolon
        # bilincli olarak update kapsaminin DISINDA birakilir:
        #   - isin: sahibi `isin` dataset'idir; kapsama girseydi ikinci
        #     symbols calistirmasi ISIN'i NULL'a ezerdi.
        #   - is_active: kullanici karari (`symbols deactivate`) ile delist
        #     sayaci ayri tutulur; kapsama girseydi basarili bir sync elle
        #     pasiflestirilmis sembolu sessizce yeniden aktiflestirirdi.
        #     Satirda yine bulunur, boylece ILK INSERT varsayilani 1 olur.
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
