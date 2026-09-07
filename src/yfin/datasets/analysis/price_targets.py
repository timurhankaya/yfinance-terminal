"""analyst_price_targets dataset.

The source returns a dict, not a DataFrame, with keys already mapped:
`target*Price` -> `low/high/mean/median`, `currentPrice` -> `current`.

No consistency constraint is enforced: THYAO was measured with
low(330) > current(294). `0.0` is a real value (not "no target") and is
not converted to NULL.
"""

from __future__ import annotations

from typing import Any

from yfin.core import normalize as nz
from yfin.core.families import DataFamily
from yfin.core.logging_setup import get_logger
from yfin.datasets.asof_base import AsOfDataset, asof_produces
from yfin.datasets.base import NormalizedResult, SyncContext
from yfin.datasets.exposure import ApiExposure
from yfin.datasets.payloads import AsOfMappingPayload
from yfin.datasets.registry import register
from yfin.ingest.client import call_optional
from yfin.storage.contracts import TableWrite

log = get_logger(__name__)

TABLE = "analyst_price_targets"
VALUE_COLUMNS = ("current", "low", "high", "mean", "median")


class AnalystPriceTargetsDataset(AsOfDataset[AsOfMappingPayload]):
    name = "analyst_price_targets"
    depends_on = ("symbols",)
    produces = asof_produces(TABLE)
    gate_source_tables = (TABLE,)
    api = (
        ApiExposure(
            family=DataFamily.FUNDAMENTALS,
            table="analyst_price_targets",
            sort_key=("as_of_date",),
            descending=True,
            description="Analyst price target range and mean.",
        ),
    )

    def fetch(self, ctx: SyncContext) -> AsOfMappingPayload:
        payload = call_optional(
            ctx.ticker.get_analyst_price_targets, what=f"{self.name}:{ctx.symbol}"
        )
        return AsOfMappingPayload(payload=payload, fetched_at=ctx.fetched_at)

    def normalize(self, raw: AsOfMappingPayload, symbol: str) -> NormalizedResult:
        payload = raw.payload
        if nz.is_empty_result(payload):
            return NormalizedResult()
        assert payload is not None

        unmapped = sorted(k for k in payload if k not in VALUE_COLUMNS)
        if unmapped:
            log.warning("unmapped keys", dataset=self.name, symbol=symbol, keys=unmapped)

        row: dict[str, Any] = {
            "symbol": symbol,
            "as_of_date": raw.fetched_at.date(),
            **{name: nz.to_decimal(payload.get(name)) for name in VALUE_COLUMNS},
            "fetched_at": raw.fetched_at,
        }
        return NormalizedResult(
            writes=[
                TableWrite(
                    table=TABLE,
                    rows=[row],
                    key_columns=("symbol", "as_of_date"),
                    update_columns=(*VALUE_COLUMNS, "fetched_at"),
                )
            ]
        )


register(AnalystPriceTargetsDataset(), family="analysis")
