"""fast_info dataset'i (S6.3 #9) -> ticker_fast_info(+_history)."""

from __future__ import annotations

from yfin.core import normalize as nz
from yfin.core.families import DataFamily
from yfin.datasets.base import NormalizedResult, SyncContext
from yfin.datasets.common import data_columns, snapshot_rows, warn_unmapped
from yfin.datasets.exposure import ApiExposure
from yfin.datasets.payloads import FastInfoPayload
from yfin.datasets.registry import register
from yfin.datasets.snapshot_base import SnapshotDataset, snapshot_writes
from yfin.datasets.symbols import fetch_fast_info
from yfin.models.fields import FAST_INFO_FIELDS

_SNAPSHOT_UPDATE = data_columns(FAST_INFO_FIELDS, extra=("raw_json", "content_hash", "fetched_at"))
_HISTORY_UPDATE = data_columns(FAST_INFO_FIELDS, extra=("raw_json", "content_hash"))


class FastInfoDataset(SnapshotDataset[FastInfoPayload]):
    name = "fast_info"
    depends_on = ("symbols",)
    produces = ("ticker_fast_info", "ticker_fast_info_history")
    snapshot_table = "ticker_fast_info"
    history_table = "ticker_fast_info_history"
    api = (
        ApiExposure(
            name="fast_info",
            family=DataFamily.REFERENCE,
            table="ticker_fast_info",
            sort_key=("symbol",),
            description="Latest lightweight quote snapshot.",
        ),
        ApiExposure(
            name="fast_info_history",
            family=DataFamily.REFERENCE,
            table="ticker_fast_info_history",
            sort_key=("fetched_at",),
            descending=True,
            description="Point-in-time history of the quote snapshot.",
        ),
    )

    def fetch(self, ctx: SyncContext) -> FastInfoPayload:
        return FastInfoPayload(fast_info=fetch_fast_info(ctx), fetched_at=ctx.fetched_at)

    def normalize(self, raw: FastInfoPayload, symbol: str) -> NormalizedResult:
        fast_info = raw.fast_info
        if fast_info is None:
            return NormalizedResult()

        # Upstream hardcodes 20 keys (quote.py:_public_keys), identical
        # across the 5 markets measured. marketCap/shares are None for
        # ETFs, crypto, FX and indices.
        # FastInfo is not a Mapping but is iterable, so an explicit loop
        # is used instead of dict()
        payload = {key: fast_info[key] for key in fast_info}
        if nz.is_empty_result(payload):
            return NormalizedResult()

        warn_unmapped(payload, FAST_INFO_FIELDS, dataset="fast_info")
        row, _ = snapshot_rows(symbol, payload, FAST_INFO_FIELDS, raw.fetched_at)

        return NormalizedResult(
            writes=snapshot_writes(
                self, [row], snapshot_update=_SNAPSHOT_UPDATE, history_update=_HISTORY_UPDATE
            )
        )


register(FastInfoDataset())
