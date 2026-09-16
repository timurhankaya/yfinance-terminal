"""institutional_holders + mutualfund_holders -> institutional_holders.

One table, separated by `holder_type`; `scope_values` is explicit so a dropped
holder is still cleared. An empty result leaves that day's rows (404 may be transient).
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
        # scope.
        scope_values = (
            {"symbol": symbol, "as_of_date": as_of, "holder_type": self.holder_type.value},
        )

        frame = raw.frame
        if nz.is_empty_result(frame):
            return NormalizedResult()
        assert isinstance(frame, pd.DataFrame)

        unmapped = sorted(str(c) for c in frame.columns if str(c) not in MAPPED_SOURCES)
        if unmapped:
            log.debug("unmapped keys", dataset=self.name, symbol=symbol, keys=unmapped)

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
                # Varies per row within one listing. Calendar label -> no tz
                # conversion.
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
