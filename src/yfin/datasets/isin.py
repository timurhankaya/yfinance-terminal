"""The isin dataset -- writes ONLY the isin column of the symbols table."""

from __future__ import annotations

from yfin.core import normalize as nz
from yfin.datasets.base import Dataset, NormalizedResult, SyncContext
from yfin.datasets.registry import register
from yfin.ingest.client import call_yahoo
from yfin.storage.contracts import TableWrite


class IsinDataset(Dataset[str | None]):
    name = "isin"
    depends_on = ("symbols",)
    produces = ("symbols",)  # `produces` is always a TABLE name

    def fetch(self, ctx: SyncContext) -> str | None:
        result: str | None = call_yahoo(ctx.ticker.get_isin, what=f"isin:{ctx.symbol}")
        return result

    def normalize(self, raw: str | None, symbol: str) -> NormalizedResult:
        # Sentinel ISIN: '-' comes back for THYAO.IS, BTC-USD, ^GSPC, GC=F, EURUSD=X
        isin = nz.to_str(raw, 16)
        if isin is None:
            return NormalizedResult(writes=[], skipped={})
        return NormalizedResult(
            writes=[
                TableWrite(
                    table="symbols",
                    rows=[{"symbol": symbol, "isin": isin}],
                    key_columns=("symbol",),
                    update_columns=("isin",),
                )
            ]
        )


register(IsinDataset())
