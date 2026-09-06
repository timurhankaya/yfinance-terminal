"""sustainability monitoring dataset.

No table (`produces = ()`): the `esgScores` module returned 404 for 19 of
19 symbols tested, across 8 sectors and 6 countries (AAPL, MSFT, KO, XOM,
TSLA, JPM, NVDA, GE, PFE, WMT, BA, INTC, DIS, F, THYAO.IS, NESN.SW, BP.L,
005930.KS, BABA). Rather than keep a table that will never fill, this
dataset just watches for the endpoint coming back and logs a warning if
data ever arrives.

Excluded from the `all` expansion (`opt_in=True`): running it for every
symbol would just produce a wasted request and an `empty` cell each time.
Registration is unconditional, but the dataset only runs when named
explicitly via `--datasets sustainability`.
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
            log.warning(
                "sustainability now returns data",
                symbol=symbol,
                shape=str(getattr(frame, "shape", None)),
                columns=[str(c) for c in getattr(frame, "columns", [])],
            )
        # Even if data does arrive, it is not written: there is no table.
        # Promoting the dataset to write is a decision for measurement to make.
        return NormalizedResult()


# Unconditional registration, gated by `opt_in=True`.
#
# This used to be `if get_settings().yf_probe_sustainability:`, the only
# module-level `get_settings()` call in the codebase. Because the import
# chain is `cli.py -> yfin.datasets -> analysis -> sustainability`, even
# `yfin --help` was forced to build Settings; once a DB layer existed, that
# meant commands touching no DB would still connect to one, and recovery
# commands would fail while the DB was down.
#
# `yf_discovery_enabled` hit the same trap and was fixed the same way (see
# the note in config.py); `sustainability` was missed in that pass.
# Behavior is unchanged: the dataset runs only when named explicitly via
# `--datasets sustainability`, never as part of `all`. The
# `yf_probe_sustainability` setting itself was removed outright, since
# nothing read it anymore.
register(SustainabilityDataset(), opt_in=True)
