"""sec_filings dataset.

Source returns `list[dict]` for US symbols; for non-US and fund/ETF symbols
it returns **{} (dict)** (`quote.py:592`). Iterating a dict with `for f in
raw` walks its keys, and `f["type"]` then raises TypeError -- hence the type
check is mandatory.
"""

from __future__ import annotations

import re
from typing import Any

from yfin.core import normalize as nz
from yfin.core.logging_setup import get_logger
from yfin.datasets.base import Dataset, NormalizedResult, SyncContext
from yfin.datasets.common import key_value
from yfin.datasets.payloads import SecFilingsPayload
from yfin.datasets.registry import register
from yfin.ingest.client import call_optional
from yfin.storage.contracts import TableWrite

log = get_logger(__name__)

# Accession number inside edgarUrl: 80/80 successful for AAPL.
ACCESSION_RE = re.compile(r"(\d{10}-\d{2}-\d{6})")

FILING_UPDATE_COLUMNS = (
    "filing_date",
    "filed_ts_utc",
    "filing_type",
    "title",
    "edgar_url",
    "exhibit_count",
    "raw_json",
    "fetched_at",
)


def _filing_id(entry: dict[str, Any]) -> str:
    url = nz.to_str(entry.get("edgarUrl")) or ""
    match = ACCESSION_RE.search(url)
    if match:
        return match.group(1)
    seed = f"{entry.get('date')}|{entry.get('type')}|{entry.get('title')}"
    return nz.content_hash(seed)[:32]


class SecFilingsDataset(Dataset[SecFilingsPayload]):
    name = "sec_filings"
    depends_on = ("symbols",)
    produces = ("sec_filings", "sec_filing_exhibits")

    def fetch(self, ctx: SyncContext) -> SecFilingsPayload:
        # 404 for non-US symbols, {} for fund/ETF; both count as empty.
        raw = ctx.cached(
            "sec_filings",
            lambda: call_optional(ctx.ticker.get_sec_filings, what=f"sec_filings:{ctx.symbol}"),
        )
        # Non-US returns {}; anything that isn't a list is treated as empty.
        filings = list(raw) if isinstance(raw, list) else []
        return SecFilingsPayload(filings=filings, fetched_at=ctx.fetched_at)

    def normalize(self, raw: SecFilingsPayload, symbol: str) -> NormalizedResult:
        if not raw.filings:
            return NormalizedResult()

        # Deduplicated by PK (same `rows[key] = ...` pattern as sibling
        # datasets). If the source repeats a `filing_id`, a plain list would
        # give `attempted=2 / verified=1` and `_record_items` would wrongly
        # mark the cell `failed` even though the data was written correctly.
        filings: dict[str, dict[str, Any]] = {}
        exhibits_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}

        for entry in raw.filings:
            if not isinstance(entry, dict):
                continue
            filing_date = nz.to_local_date(entry.get("date"))
            # epochDate is in SECONDS (1788220800 -> 2026-09-01).
            filed_ts = nz.epoch_to_datetime(entry.get("epochDate"), unit="s")
            filing_type = nz.to_str(entry.get("type"), max_len=32)
            if filing_date is None or filed_ts is None or filing_type is None:
                # A filing missing a required field is SKIPPED; writing it
                # would raise a NOT NULL violation (23502) and drop the
                # symbol's ENTIRE transaction.
                log.warning(
                    "sec filing missing required field", symbol=symbol, entry=str(entry)[:120]
                )
                continue

            filing_id = _filing_id(entry)
            exhibits = entry.get("exhibits")
            if not isinstance(exhibits, dict):
                exhibits = {}
            for exhibit_type, url in exhibits.items():
                # PK component -> NOT truncated.
                type_key = key_value(
                    exhibit_type, 32, field="exhibit_type", dataset=self.name, symbol=symbol
                )
                url_text = nz.to_str(url)
                if type_key is None or url_text is None:
                    continue
                url_hash = nz.content_hash(url_text)[:16]
                exhibits_by_key[(filing_id, type_key, url_hash)] = {
                    "symbol": symbol,
                    "filing_id": filing_id,
                    "exhibit_type": type_key,
                    # url is TEXT so it cannot join the PK (btree tuple limit);
                    # the same filing can have two EX-99.1 entries with
                    # different URLs.
                    "url_hash": url_hash,
                    "url": url_text,
                }

            filings[filing_id] = (
                {
                    "symbol": symbol,
                    "filing_id": filing_id,
                    "filing_date": filing_date,
                    "filed_ts_utc": filed_ts,
                    "filing_type": filing_type,
                    "title": nz.to_str(entry.get("title")),
                    "edgar_url": nz.to_str(entry.get("edgarUrl")),
                    # yfinance overwrites same-type exhibits when building its
                    # {type: url} dict, so this count is the number of DISTINCT
                    # exhibit types.
                    "exhibit_count": len(exhibits),
                    "raw_json": nz.canonical_json(entry),
                    "fetched_at": raw.fetched_at,
                }
            )

        if not filings:
            return NormalizedResult()

        filing_rows = list(filings.values())
        exhibit_rows = list(exhibits_by_key.values())
        writes = [
            TableWrite(
                table="sec_filings",
                rows=filing_rows,
                key_columns=("symbol", "filing_id"),
                update_columns=FILING_UPDATE_COLUMNS,
            ),
            TableWrite(
                table="sec_filing_exhibits",
                rows=exhibit_rows,
                key_columns=("symbol", "filing_id", "exhibit_type", "url_hash"),
                update_columns=("url",),
                mode="replace_scope",
                scope_columns=("symbol", "filing_id"),
                scope_values=tuple(
                    {"symbol": row["symbol"], "filing_id": row["filing_id"]} for row in filing_rows
                ),
            ),
        ]
        return NormalizedResult(writes=writes)


register(SecFilingsDataset())
