"""isin dataset'i (S6.3 #1) - symbols tablosunun YALNIZ isin kolonuna yazar."""

from __future__ import annotations

from yfin.core import normalize as nz
from yfin.datasets.base import Dataset, NormalizedResult, SyncContext, TableWrite
from yfin.datasets.registry import register
from yfin.ingest.client import call_yahoo


class IsinDataset(Dataset[str | None]):
    name = "isin"
    depends_on = ("symbols",)
    produces = ("symbols",)  # produces her zaman TABLO adidir (S6.1/1)

    def fetch(self, ctx: SyncContext) -> str | None:
        result: str | None = call_yahoo(ctx.ticker.get_isin, what=f"isin:{ctx.symbol}")
        return result

    def normalize(self, raw: str | None, symbol: str) -> NormalizedResult:
        # Sentinel ISIN: '-' THYAO.IS, BTC-USD, ^GSPC, GC=F, EURUSD=X'te gelir
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
