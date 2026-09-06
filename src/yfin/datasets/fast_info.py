"""fast_info dataset'i (S6.3 #9) -> ticker_fast_info(+_history)."""

from __future__ import annotations

from yfin.core import normalize as nz
from yfin.datasets.base import NormalizedResult, SyncContext
from yfin.datasets.common import data_columns, snapshot_rows, warn_unmapped
from yfin.datasets.payloads import FastInfoPayload
from yfin.datasets.registry import register
from yfin.datasets.snapshot_base import SnapshotDataset
from yfin.datasets.symbols import fetch_fast_info
from yfin.models.fields import FAST_INFO_FIELDS
from yfin.storage.contracts import TableWrite

_SNAPSHOT_UPDATE = data_columns(FAST_INFO_FIELDS, extra=("raw_json", "content_hash", "fetched_at"))
_HISTORY_UPDATE = data_columns(FAST_INFO_FIELDS, extra=("raw_json", "content_hash"))


class FastInfoDataset(SnapshotDataset[FastInfoPayload]):
    name = "fast_info"
    depends_on = ("symbols",)
    produces = ("ticker_fast_info", "ticker_fast_info_history")
    snapshot_table = "ticker_fast_info"
    history_table = "ticker_fast_info_history"

    def fetch(self, ctx: SyncContext) -> FastInfoPayload:
        return FastInfoPayload(fast_info=fetch_fast_info(ctx), fetched_at=ctx.fetched_at)

    def normalize(self, raw: FastInfoPayload, symbol: str) -> NormalizedResult:
        fast_info = raw.fast_info
        if fast_info is None:
            return NormalizedResult()

        # Kaynakta hardcoded 20 anahtar (quote.py:_public_keys), 5 pazarda
        # birebir ayni. marketCap/shares ETF, kripto, FX ve endekste None.
        # FastInfo bir Mapping degildir ama iterable'dir; dict() yerine
        # acik dongu kullanilir
        payload = {key: fast_info[key] for key in fast_info}
        if nz.is_empty_result(payload):
            return NormalizedResult()

        warn_unmapped(payload, FAST_INFO_FIELDS, dataset="fast_info")
        row, _ = snapshot_rows(symbol, payload, FAST_INFO_FIELDS, raw.fetched_at)
        history_row = dict(row)

        return NormalizedResult(
            writes=[
                TableWrite(
                    table="ticker_fast_info",
                    rows=[row],
                    key_columns=("symbol",),
                    update_columns=_SNAPSHOT_UPDATE,
                ),
                TableWrite(
                    table="ticker_fast_info_history",
                    rows=[history_row],
                    key_columns=("symbol", "fetched_at"),
                    update_columns=_HISTORY_UPDATE,
                ),
            ]
        )


register(FastInfoDataset())
