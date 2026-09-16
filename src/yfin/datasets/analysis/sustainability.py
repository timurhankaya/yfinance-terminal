"""sustainability monitoring dataset.

No table: the `esgScores` module 404s for every symbol, so this only warns
if data ever arrives. Opt-in, so `all` does not spend a request on it.
"""

from __future__ import annotations

from yfin.core import normalize as nz
from yfin.core.logging_setup import get_logger
from yfin.datasets.base import Dataset, NormalizedResult, SyncContext
from yfin.datasets.payloads import AsOfFramePayload
from yfin.datasets.registry import register
from yfin.ingest.client import call_optional

log = get_logger(__name__)


class SustainabilityDataset(Dataset[AsOfFramePayload]):
    name = "sustainability"
    depends_on = ("symbols",)
    # Writes to no table. `runner._record_items` guards against this empty
    # tuple with `or dataset.produces or [None]`; otherwise the dataset would
    # disappear from auditing entirely.
    produces = ()

    def fetch(self, ctx: SyncContext) -> AsOfFramePayload:
        frame = call_optional(ctx.ticker.get_sustainability, what=f"{self.name}:{ctx.symbol}")
        return AsOfFramePayload(frame=frame, fetched_at=ctx.fetched_at)

    def normalize(self, raw: AsOfFramePayload, symbol: str) -> NormalizedResult:
        frame = raw.frame
        if not nz.is_empty_result(frame):
            assert frame is not None
            log.debug(
                "sustainability now returns data",
                symbol=symbol,
                shape=str(getattr(frame, "shape", None)),
                columns=[str(c) for c in getattr(frame, "columns", [])],
            )
        return NormalizedResult()


# Registration must not consult `get_settings()`: this module is imported by
# `yfin --help`, and building Settings at import time would touch the DB.
register(SustainabilityDataset(), opt_in=True)
