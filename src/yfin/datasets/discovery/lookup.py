"""lookup dataset -> symbols | lookup_results | lookup_totals.

The call is adaptive, which is a decision measurement overturned. The
first cut always made one `all` call, and was generalized from a single
narrow term (`BTC`, total 503). Independent verification found the `all`
call gets hard-clipped around ~1,000 documents for broad terms:

    term    typed union   `all`   `all`-only   typed-only
    BTC          500        500        0            0
    GOLD       3,313        996      354        2,671
    TECH       4,024        998        0        3,026

For `GOLD` the difference runs both ways: 354 symbols (all `0P...` fund
codes) appear only in `all`, 2,671 only in the typed calls. So switching
to typed calls means adding to `all`, not replacing it.

The adaptive branch almost never fires in the per-symbol loop (57 for
AAPL, 1 for THYAO), so cost stays at 1 request/symbol; only broad
free-text terms pay for it, and get 3-4x the symbols in return.

Uses `_fetch_lookup` (raw body), not `get_all()` (DataFrame):
`Lookup._parse_response` discards the `lookupTotals` and `total` fields,
but the `lookup_totals` table -- proof of completeness and the adaptive
branch's trigger -- depends on them. `_fetch_lookup` is the wrapper's own
method, so it still handles HTTP and the proxy itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import yfinance as yf

from yfin.core import normalize as nz
from yfin.core.config import get_settings
from yfin.core.families import DataFamily
from yfin.core.logging_setup import get_logger
from yfin.datasets.asof_base import asof_produces
from yfin.datasets.base import NormalizedResult, SyncContext
from yfin.datasets.discovery.base import DISCOVERY_GATE_TABLE, DiscoveryDataset
from yfin.datasets.exposure import ApiExposure
from yfin.datasets.registry import register
from yfin.datasets.symbol_discovery import (
    dict_items,
    discovered_symbol_row,
    expect_dict,
    symbol_is_writable,
    utc_as_of_day,
)
from yfin.ingest.client import call_yahoo
from yfin.storage.contracts import TableWrite

log = get_logger(__name__)

ALL_TYPE = "all"
# The non-`all` members of yfinance's `LOOKUP_TYPES` constant. Kept here
# rather than imported because that upstream list is unaware of the ninth
# type (`privateCompany`), and if the library adds it one day, our call set
# should not silently change with it.
TYPED_LOOKUPS = (
    "equity",
    "mutualfund",
    "etf",
    "index",
    "future",
    "currency",
    "cryptocurrency",
)

_RESULT_UPDATE = (
    "rank_index",
    "source_rank",
    "lookup_type",
    "quote_type",
    "exchange",
    "short_name",
    "industry_name",
    "industry_link",
    "fullday_price",
    "fullday_change",
    "fullday_change_percent",
    "regular_market_price",
    "regular_market_change",
    "regular_market_percent_change",
    "is_known",
    "fetched_at",
    "raw_json",
)

_TOTAL_UPDATE = ("total", "fetched_at")

# `lookup` only returns three identifying fields. Using a shared `symbols`
# update list would NULL `long_name`, `currency`, `timezone`, and
# `full_exchange_name` on every run, i.e. erase what `search`/`screener` wrote.
SYMBOL_UPDATE = ("short_name", "exchange", "quote_type", "last_seen_at")


@dataclass
class LookupPayload:
    query_term: str
    as_of_date: date
    fetched_at: datetime
    # (lookup_type, document) pairs; order is preserved
    documents: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    totals: dict[str, int] = field(default_factory=dict)


def _result_block(payload: Any) -> dict[str, Any]:
    """Unwraps the `{"finance": {"result": [ ... ]}}` envelope.

    Raises KeyError/TypeError if the envelope shape changes, marking the
    cell `failed`; returning empty silently would conflate "no data" with
    "the response shape changed".
    """
    result = expect_dict(payload, what="lookup").get("finance", {}).get("result") or []
    return result[0] if result else {}


def _fetch_type(term: str, lookup_type: str, count: int) -> dict[str, Any]:
    return _result_block(
        call_yahoo(
            lambda: yf.Lookup(term)._fetch_lookup(lookup_type, count),
            what=f"lookup:{term}:{lookup_type}",
        )
    )


class LookupDataset(DiscoveryDataset[LookupPayload]):
    name = "lookup"
    produces = asof_produces(
        "symbols", "lookup_results", "lookup_totals", gate=DISCOVERY_GATE_TABLE
    )
    api = (
        ApiExposure(
            name="lookup_results",
            family=DataFamily.DISCOVERY,
            table="lookup_results",
            sort_key=("as_of_date", "query_term", "symbol"),
            descending=True,
            filters=("query_term",),
            symbol_optional=True,
            description="Symbols a lookup query returned.",
        ),
        ApiExposure(
            name="lookup_totals",
            family=DataFamily.DISCOVERY,
            table="lookup_totals",
            sort_key=("as_of_date", "query_term", "lookup_type"),
            descending=True,
            filters=("query_term", "lookup_type"),
            description="How many results a lookup had, by type.",
        ),
    )

    def fetch(self, ctx: SyncContext) -> LookupPayload:
        cfg = get_settings()
        term = ctx.symbol
        block = _fetch_type(term, ALL_TYPE, cfg.yf_lookup_count)
        totals = {
            str(k): int(v)
            for k, v in (block.get("lookupTotals") or {}).items()
            if isinstance(v, int)
        }
        docs: list[tuple[str, dict[str, Any]]] = [
            (ALL_TYPE, d) for d in dict_items(block, "documents")
        ]

        # If `lookupTotals.all` exceeds the threshold, `all` is clipped. The
        # threshold is kept below the observed `all` cap (~1,000) so the
        # typed branch kicks in before clipping starts.
        if totals.get(ALL_TYPE, 0) > cfg.yf_lookup_all_threshold:
            log.info(
                "lookup all was truncated, switching to the typed branch",
                term=term,
                total=totals.get(ALL_TYPE),
                documents=len(docs),
            )
            for lookup_type in TYPED_LOOKUPS:
                typed = _fetch_type(term, lookup_type, cfg.yf_lookup_count)
                docs.extend((lookup_type, d) for d in dict_items(typed, "documents"))

        return LookupPayload(
            query_term=term,
            as_of_date=utc_as_of_day(ctx.fetched_at),
            fetched_at=ctx.fetched_at,
            documents=docs,
            totals=totals,
        )

    def normalize(self, raw: LookupPayload, symbol: str) -> NormalizedResult:
        results: list[dict[str, Any]] = []
        symbols: list[dict[str, Any]] = []
        seen: set[str] = set()

        for index, (lookup_type, doc) in enumerate(raw.documents):
            sym = nz.to_str(doc.get("symbol"))
            if sym is None or sym in seen:
                # The same symbol can come back from both the `all` call
                # and a typed call (measured: three symbols under two types
                # for BTC). Since the PK is (query_term, as_of_date,
                # symbol), the second row would overwrite the first; the
                # first seen -- i.e. the one from `all` -- is kept instead.
                continue
            seen.add(sym)
            is_known = symbol_is_writable(sym)
            results.append(
                {
                    "query_term": raw.query_term,
                    "as_of_date": raw.as_of_date,
                    "symbol": sym,
                    "rank_index": index,
                    # The source's own `rank` field is not a position but
                    # Yahoo's ranking score (measured example: 30007). The
                    # column name is split apart for this reason; the
                    # source key is still `rank`.
                    "source_rank": nz.to_int(doc.get("rank")),
                    "lookup_type": lookup_type,
                    "quote_type": nz.to_str(doc.get("quoteType"), max_len=32),
                    "exchange": nz.to_str(doc.get("exchange"), max_len=32),
                    "short_name": nz.to_str(doc.get("shortName"), max_len=128),
                    # Populated only for `equity` documents
                    "industry_name": nz.to_str(doc.get("industryName"), max_len=128),
                    "industry_link": nz.to_str(doc.get("industryLink")),
                    "fullday_price": nz.to_decimal(doc.get("fulldayPrice")),
                    "fullday_change": nz.to_decimal(doc.get("fulldayChange")),
                    "fullday_change_percent": nz.to_decimal(doc.get("fulldayChangePercent")),
                    "regular_market_price": nz.to_decimal(doc.get("regularMarketPrice")),
                    "regular_market_change": nz.to_decimal(doc.get("regularMarketChange")),
                    "regular_market_percent_change": nz.to_decimal(
                        doc.get("regularMarketPercentChange")
                    ),
                    "is_known": is_known,
                    "fetched_at": raw.fetched_at,
                    "raw_json": nz.canonical_json(doc),
                }
            )
            if is_known:
                symbols.append(
                    discovered_symbol_row(
                        sym,
                        source="lookup",
                        fetched_at=raw.fetched_at,
                        # A lookup document only carries three identifying
                        # fields; `SYMBOL_UPDATE` is limited to those same three.
                        short_name=nz.to_str(doc.get("shortName"), max_len=128),
                        exchange=nz.to_str(doc.get("exchange"), max_len=32),
                        quote_type=nz.to_str(doc.get("quoteType"), max_len=32),
                    )
                )

        totals = [
            {
                "query_term": raw.query_term,
                "as_of_date": raw.as_of_date,
                "lookup_type": lookup_type,
                "total": total,
                "fetched_at": raw.fetched_at,
            }
            # The source reports nine types (including `privateCompany`),
            # which `LOOKUP_TYPES` is unaware of, so the set is read from
            # the response, not the constant.
            for lookup_type, total in sorted(raw.totals.items())
        ]

        return NormalizedResult(
            writes=[
                TableWrite(
                    table="symbols",
                    rows=symbols,
                    key_columns=("symbol",),
                    update_columns=SYMBOL_UPDATE,
                ),
                TableWrite(
                    table="lookup_results",
                    rows=results,
                    key_columns=("query_term", "as_of_date", "symbol"),
                    update_columns=_RESULT_UPDATE,
                ),
                TableWrite(
                    table="lookup_totals",
                    rows=totals,
                    key_columns=("query_term", "as_of_date", "lookup_type"),
                    update_columns=_TOTAL_UPDATE,
                ),
            ]
        )


# Opt-in: registered but excluded from the `all` expansion.
# `yfin sync --datasets lookup` runs it; a bare `yfin sync` does not fetch
# it, so its cost is unchanged.
register(LookupDataset(), opt_in=True)
