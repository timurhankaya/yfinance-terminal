"""history_metadata dataset -> history_metadata."""

from __future__ import annotations

from yfin.core import normalize as nz
from yfin.core.families import DataFamily
from yfin.datasets.base import Dataset, NormalizedResult, SyncContext
from yfin.datasets.common import data_columns, snapshot_rows, warn_unmapped
from yfin.datasets.exposure import ApiExposure
from yfin.datasets.payloads import MetadataPayload
from yfin.datasets.registry import register
from yfin.datasets.symbols import fetch_history_metadata
from yfin.models.fields import HISTORY_METADATA_FIELDS
from yfin.storage.contracts import TableWrite

# Keys kept only in raw_json; never promoted to a column
_RAW_ONLY = frozenset(
    {"symbol", "tradingPeriods", "currentTradingPeriod", "validRanges", "YF repair?"}
)


class HistoryMetadataDataset(Dataset[MetadataPayload]):
    name = "history_metadata"
    depends_on = ("symbols",)
    produces = ("history_metadata",)
    api = (
        ApiExposure(
            family=DataFamily.REFERENCE,
            table="history_metadata",
            sort_key=("symbol",),
            description="Exchange, timezone and trading-period metadata.",
        ),
    )

    def fetch(self, ctx: SyncContext) -> MetadataPayload:
        return MetadataPayload(metadata=fetch_history_metadata(ctx), fetched_at=ctx.fetched_at)

    def normalize(self, raw: MetadataPayload, symbol: str) -> NormalizedResult:
        metadata = raw.metadata
        if nz.is_empty_result(metadata):
            return NormalizedResult()

        # HistoryMetadata is NOT a dict, it is a Mapping
        payload = nz.as_mapping(metadata)
        warn_unmapped(
            payload, HISTORY_METADATA_FIELDS, dataset="history_metadata", ignore=_RAW_ONLY
        )
        # tradingPeriods is a DataFrame; YFJSONEncoder turns it into records
        row, _ = snapshot_rows(symbol, payload, HISTORY_METADATA_FIELDS, raw.fetched_at)

        return NormalizedResult(
            writes=[
                TableWrite(
                    table="history_metadata",
                    rows=[row],
                    key_columns=("symbol",),
                    update_columns=data_columns(
                        HISTORY_METADATA_FIELDS,
                        extra=("raw_json", "content_hash", "fetched_at"),
                    ),
                )
            ]
        )


register(HistoryMetadataDataset())
