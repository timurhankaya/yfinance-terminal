"""info dataset -> ticker_info, ticker_info_history, company_officers."""

from __future__ import annotations

from typing import Any

from yfin.core import normalize as nz
from yfin.core.families import DataFamily
from yfin.datasets.base import NormalizedResult, SyncContext
from yfin.datasets.common import data_columns, snapshot_rows, warn_unmapped
from yfin.datasets.exposure import ApiExposure
from yfin.datasets.payloads import InfoPayload
from yfin.datasets.registry import register
from yfin.datasets.snapshot_base import SnapshotDataset
from yfin.ingest.client import call_yahoo
from yfin.models.fields import INFO_FIELDS, INFO_NESTED_KEYS
from yfin.storage.contracts import TableWrite

_SNAPSHOT_UPDATE = data_columns(INFO_FIELDS, extra=("raw_json", "content_hash", "fetched_at"))
_HISTORY_UPDATE = data_columns(INFO_FIELDS, extra=("raw_json", "content_hash"))

_OFFICER_COLUMNS = (
    "title",
    "age",
    "year_born",
    "fiscal_year",
    "total_pay",
    "exercised_value",
    "unexercised_value",
)

# companyOfficers goes to a table; corporateActions, executiveTeam, and
# maxAge stay in raw_json.
_IGNORE_FOR_WARNING = INFO_NESTED_KEYS | {"maxAge", "symbol", "uuid"}


def _officer_rows(symbol: str, officers: Any) -> list[dict[str, Any]]:
    if nz.is_empty_result(officers):
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in officers:
        raw_name = entry.get("name") if hasattr(entry, "get") else None
        name = nz.to_str(raw_name, 255)
        if name is None:
            continue
        # Source has double spaces ("Mr. Kevan  Parekh"); without
        # normalizing, a Yahoo whitespace change would produce a duplicate row.
        name = nz.normalize_person_name(name)
        if name in seen:
            continue
        seen.add(name)
        rows.append(
            {
                "symbol": symbol,
                "name": name,
                "title": nz.to_str(entry.get("title"), 255),
                # age, yearBorn, and totalPay are OPTIONAL in the source.
                "age": nz.to_int(entry.get("age")),
                "year_born": nz.to_int(entry.get("yearBorn")),
                "fiscal_year": nz.to_int(entry.get("fiscalYear")),
                "total_pay": nz.to_decimal(entry.get("totalPay")),
                "exercised_value": nz.to_decimal(entry.get("exercisedValue")),
                "unexercised_value": nz.to_decimal(entry.get("unexercisedValue")),
            }
        )
    return rows


class InfoDataset(SnapshotDataset[InfoPayload]):
    name = "info"
    depends_on = ("symbols",)
    produces = ("ticker_info", "ticker_info_history", "company_officers")
    snapshot_table = "ticker_info"
    history_table = "ticker_info_history"
    # The snapshot itself is served by /v1/symbols/{symbol}; what was
    # unreachable is the officer roster and the point-in-time history.
    api = (
        ApiExposure(
            name="company_officers",
            family=DataFamily.REFERENCE,
            table="company_officers",
            sort_key=("name",),
            description="Named officers and their compensation.",
        ),
        ApiExposure(
            name="info_history",
            family=DataFamily.REFERENCE,
            table="ticker_info_history",
            sort_key=("fetched_at",),
            descending=True,
            description="Point-in-time history of the identity snapshot.",
        ),
    )

    def fetch(self, ctx: SyncContext) -> InfoPayload:
        info = ctx.cached(
            "info", lambda: call_yahoo(ctx.ticker.get_info, what=f"info:{ctx.symbol}")
        )
        return InfoPayload(info=info, fetched_at=ctx.fetched_at)

    def normalize(self, raw: InfoPayload, symbol: str) -> NormalizedResult:
        info = raw.info
        if info is None or nz.is_empty_result(info):
            return NormalizedResult()

        payload = dict(info)
        # Field set varies by symbol (AAPL 187, BTC-USD 91); raw data stays
        # in raw_json so nothing is lost, the log is only a signal to investigate.
        warn_unmapped(payload, INFO_FIELDS, dataset="info", ignore=_IGNORE_FOR_WARNING)
        nz.warn_unmapped_epoch_like(payload, nz.EPOCH_SEC_FIELDS | nz.EPOCH_MS_FIELDS)

        row, _ = snapshot_rows(symbol, payload, INFO_FIELDS, raw.fetched_at)
        history_row = dict(row)
        officers = _officer_rows(symbol, payload.get("companyOfficers"))

        writes = [
            TableWrite(
                table="ticker_info",
                rows=[row],
                key_columns=("symbol",),
                update_columns=_SNAPSHOT_UPDATE,
            ),
            TableWrite(
                table="ticker_info_history",
                rows=[history_row],
                key_columns=("symbol", "fetched_at"),
                update_columns=_HISTORY_UPDATE,
            ),
        ]
        if officers:
            writes.append(
                TableWrite(
                    table="company_officers",
                    rows=officers,
                    key_columns=("symbol", "name"),
                    update_columns=_OFFICER_COLUMNS,
                    # An officer who has left the company should not stay forever.
                    mode="replace_scope",
                )
            )

        return NormalizedResult(writes=writes)


register(InfoDataset())
