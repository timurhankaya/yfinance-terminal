"""institutional_holders + mutualfund_holders -> institutional_holders.

The two datasets write to the SAME table; column sets measured identical
across 14 symbols. Their scopes are separated by
`scope_columns=(symbol, as_of_date, holder_type)` -- this is exactly why
`scope_columns` exists. They stay separate registrations because each needs
its own `sync_run_items` cell and its own `--datasets` selectability.

`scope_values` is given EXPLICITLY: if scope were derived from the rows,
dropping one holder from the source would leave its stale row outside scope
forever.

KNOWN LIMITATION: if the source comes back entirely empty (empty frame or
404), `normalize` returns early and `AsOfDataset.upsert` sees `is_empty` and
writes nothing; that day's EXISTING rows are NOT cleared. This is
DELIBERATE: `call_optional` cannot tell a transient 404 apart from a genuine
"zero holders" state, and deleting would destroy that as-of day's data on a
transient outage. The cost: running twice in one day where the second run
comes back empty leaves the morning's rows under that day's label.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from yfin.core import normalize as nz
from yfin.core.families import DataFamily
from yfin.core.logging_setup import get_logger
from yfin.datasets.asof_base import AsOfDataset, asof_produces
from yfin.datasets.base import NormalizedResult, SyncContext
from yfin.datasets.common import key_value
from yfin.datasets.exposure import ApiExposure
from yfin.datasets.payloads import AsOfFramePayload
from yfin.datasets.registry import register
from yfin.ingest.client import call_optional
from yfin.models.holders import HolderType
from yfin.storage.contracts import TableWrite

log = get_logger(__name__)

TABLE = "institutional_holders"
HOLDER_LENGTH = 128
KEY_COLUMNS = ("symbol", "as_of_date", "holder_type", "holder")
SCOPE_COLUMNS = ("symbol", "as_of_date", "holder_type")
DATA_COLUMNS = ("date_reported", "pct_held", "pct_change", "shares", "value")
MAPPED_SOURCES = frozenset({"Date Reported", "Holder", "pctHeld", "pctChange", "Shares", "Value"})


class _HolderListDataset(AsOfDataset[AsOfFramePayload]):
    depends_on = ("symbols",)
    produces = asof_produces(TABLE)
    gate_source_tables = (TABLE,)
    holder_type: HolderType
    api_method: str

    def fetch(self, ctx: SyncContext) -> AsOfFramePayload:
        frame = call_optional(
            getattr(ctx.ticker, self.api_method), what=f"{self.name}:{ctx.symbol}"
        )
        return AsOfFramePayload(frame=frame, fetched_at=ctx.fetched_at)

    def normalize(self, raw: AsOfFramePayload, symbol: str) -> NormalizedResult:
        as_of = raw.fetched_at.date()
        # Scope values are produced HERE, not from the rows, so that dropping
        # one holder from the source doesn't leave its stale row out of
        # scope. The source going entirely empty is a separate case; see the
        # module docstring's "KNOWN LIMITATION" note.
        scope_values = (
            {"symbol": symbol, "as_of_date": as_of, "holder_type": self.holder_type.value},
        )

        frame = raw.frame
        if nz.is_empty_result(frame):
            return NormalizedResult()
        assert isinstance(frame, pd.DataFrame)

        unmapped = sorted(str(c) for c in frame.columns if str(c) not in MAPPED_SOURCES)
        if unmapped:
            log.warning("unmapped keys", dataset=self.name, symbol=symbol, keys=unmapped)

        rows: dict[str, dict[str, Any]] = {}
        for _, record in frame.iterrows():
            holder = key_value(
                record.get("Holder"),
                HOLDER_LENGTH,
                field="holder",
                dataset=self.name,
                symbol=symbol,
            )
            if holder is None:
                continue
            rows[holder] = {
                "symbol": symbol,
                "as_of_date": as_of,
                # ENUM value is ALWAYS produced lowercase; never relies on a
                # silent column-level conversion ('INSTITUTION' -> 'institution').
                "holder_type": self.holder_type.value,
                "holder": holder,
                # Varies PER ROW: AAPL's mutualfund list has four different
                # dates in one listing. Calendar label -> no tz conversion.
                "date_reported": nz.to_local_date(record.get("Date Reported")),
                "pct_held": nz.to_decimal(record.get("pctHeld")),
                "pct_change": nz.to_decimal(record.get("pctChange")),
                "shares": nz.to_decimal(record.get("Shares")),
                "value": nz.to_decimal(record.get("Value")),
                "fetched_at": raw.fetched_at,
            }

        return NormalizedResult(
            writes=[
                TableWrite(
                    table=TABLE,
                    rows=list(rows.values()),
                    key_columns=KEY_COLUMNS,
                    update_columns=(*DATA_COLUMNS, "fetched_at"),
                    mode="replace_scope",
                    scope_columns=SCOPE_COLUMNS,
                    scope_values=scope_values,
                )
            ]
        )


class InstitutionalHoldersDataset(_HolderListDataset):
    name = "institutional_holders"
    api_method = "get_institutional_holders"
    holder_type = HolderType.INSTITUTION
    # `fixed` is what keeps the two datasets apart on the read surface:
    # both write this table and only holder_type tells them apart, so
    # without it asking for one would also return the other's rows.
    api = (
        ApiExposure(
            family=DataFamily.HOLDERS,
            table=TABLE,
            sort_key=("as_of_date", "holder"),
            descending=True,
            fixed=(("holder_type", HolderType.INSTITUTION.value),),
            description="Institutional holders and their reported positions.",
        ),
    )


class MutualFundHoldersDataset(_HolderListDataset):
    name = "mutualfund_holders"
    api_method = "get_mutualfund_holders"
    holder_type = HolderType.MUTUALFUND
    api = (
        ApiExposure(
            family=DataFamily.HOLDERS,
            table=TABLE,
            sort_key=("as_of_date", "holder"),
            descending=True,
            fixed=(("holder_type", HolderType.MUTUALFUND.value),),
            description="Mutual fund holders and their reported positions.",
        ),
    )


register(InstitutionalHoldersDataset(), group="holders")
register(MutualFundHoldersDataset(), group="holders")
