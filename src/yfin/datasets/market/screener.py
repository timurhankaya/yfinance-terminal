"""screener dataset -> screens | screen_runs | screen_members |
screen_quotes | symbols.

`scope="variant"`: the screen loop runs OUTSIDE the dataset, same as the
region loop. This makes `sync_run_items` granularity naturally
(dataset x screen x table), and one screen failing doesn't mark a
neighboring screen `failed`.

The gate is built with the `HashGate` mixin, NOT `HashGatedDataset`: that
class sits under `Dataset[RawT]` with the `fetch(SyncContext)` signature,
while this uses the `GlobalDataset` hierarchy. The gate logic is identical
in both, hence the shared mixin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import yfinance as yf

from yfin.core import normalize as nz
from yfin.core.config import Settings, get_settings
from yfin.core.families import DataFamily
from yfin.core.logging_setup import get_logger
from yfin.core.text import comma_list
from yfin.datasets.base import NormalizedResult
from yfin.datasets.common import (
    project_fields,
    warn_unmapped,
)
from yfin.datasets.exposure import ApiExposure
from yfin.datasets.hash_gated import HashGate
from yfin.datasets.market.base import GlobalDataset, MarketContext
from yfin.datasets.registry import register_market
from yfin.datasets.symbol_discovery import (
    dict_items,
    discovered_symbol_row,
    expect_dict,
    symbol_is_writable,
    utc_as_of_day,
)
from yfin.ingest.client import call_yahoo
from yfin.ingest.screens import ALL_SCREENS, ScreenDef, screen_by_key
from yfin.models.fields import SCREENER_NON_COLUMN_SOURCES, SCREENER_QUOTE_FIELDS
from yfin.storage.contracts import TableWrite, VariantState

log = get_logger(__name__)

GATE_TABLE = "screen_runs"
CHILD_TABLE = "screen_members"
GATE_KEY_COLUMNS = ("screen_key", "as_of_date")

_SCREEN_UPDATE = (
    "kind",
    "quote_type",
    "title",
    "description",
    "sort_field",
    "sort_asc",
    "definition_json",
    "updated_at",
)
# `is_enabled` is OUT OF SCOPE: if it were included, every run would flip a
# screen the operator disabled in the DB back on. `created_at` is also
# excluded (written only on INSERT).

_RUN_UPDATE = (
    "total",
    "fetched_rows",
    "row_count",
    "page_count",
    "yahoo_id",
    "version_id",
    "last_updated",
    "criteria_json",
    "content_hash",
    "fetched_at",
)

_MEMBER_UPDATE = ("rank_index", "is_known", "fetched_at")

_QUOTE_UPDATE = tuple(f.column for f in SCREENER_QUOTE_FIELDS) + (
    "is_known",
    "fetched_at",
    "raw_json",
)

# The discovery write does NOT update `is_active`, `unknown_streak`,
# `discovered_by`, or `discovered_at`. Otherwise a symbol an operator
# manually reactivated would SILENTLY go inactive again the next day it
# shows up in the same screen.
#
# The list is limited to columns the screener ACTUALLY populates: a shared
# list would make the `lookup` path NULL out `long_name`/`currency`.
SYMBOL_UPDATE = (
    "short_name",
    "long_name",
    "exchange",
    "full_exchange_name",
    "quote_type",
    "currency",
    "timezone",
    "first_trade_date",
    "last_seen_at",
)


@dataclass
class ScreenPage:
    """A single `yf.screen` response."""

    quotes: list[dict[str, Any]]
    total: int
    # Populated only on the FIRST predefined page (GET); the POST response
    # carries 5 keys and includes none of these.
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScreenPayload:
    screen_key: str
    as_of_date: date
    fetched_at: datetime
    quotes: list[dict[str, Any]]
    total: int
    page_count: int
    metadata: dict[str, Any] = field(default_factory=dict)


def _fetch_page(
    spec: ScreenDef, *, offset: int | None, size: int
) -> ScreenPage:
    """Fetches one page.

    The FIRST page uses `count`, later ones use `size`. When `offset` is
    given, `yf.screen` switches from the predefined GET path to the custom
    POST path and SILENTLY ignores `count`: `offset=250, count=250`
    returned 25 rows, `size=250` returned 250. No error is raised; using a
    single parameter name throughout would silently drop 225 rows per page.

    `sortField`/`sortAsc` are given EXPLICITLY on every request. `sortAsc`
    defaults to None -> descending; if the order isn't stable across pages,
    pages overlap or symbols get skipped.
    """
    query: Any = spec.key if spec.kind == "predefined" else spec.query
    if offset is None:
        raw = call_yahoo(
            lambda: yf.screen(
                query, count=size, sortField=spec.sort_field, sortAsc=spec.sort_asc
            ),
            what=f"screen:{spec.key}:p0",
        )
    else:
        raw = call_yahoo(
            lambda: yf.screen(
                query,
                offset=offset,
                size=size,
                sortField=spec.sort_field,
                sortAsc=spec.sort_asc,
            ),
            what=f"screen:{spec.key}:@{offset}",
        )
    body = expect_dict(raw, what="screen")
    return ScreenPage(
        quotes=dict_items(body, "quotes"),
        total=nz.to_int(body.get("total")) or 0,
        metadata={k: v for k, v in body.items() if k != "quotes"},
    )


class ScreenerDataset(HashGate, GlobalDataset[ScreenPayload]):
    name = "screener"
    scope = "variant"
    produces = (
        "screens",
        "symbols",
        "screen_quotes",
        GATE_TABLE,
        CHILD_TABLE,
    )
    api = (
        ApiExposure(
            name="screens",
            family=DataFamily.DISCOVERY,
            table="screens",
            sort_key=("screen_key",),
            filters=("kind", "quote_type"),
            symbol_optional=True,
            # The screen's own query body is our configuration, not
            # product data -- the same reason `settings` is not readable.
            # Everything else on the row is what makes the other three
            # resources usable: `screen_key` is a filter on all of them,
            # and nothing else told a caller which keys exist.
            hidden=("definition_json",),
            description="The screens this deployment runs, and what each one is.",
        ),
        ApiExposure(
            name="screen_members",
            family=DataFamily.DISCOVERY,
            table="screen_members",
            sort_key=("as_of_date", "screen_key", "symbol"),
            descending=True,
            filters=("screen_key",),
            symbol_optional=True,
            description="Symbols a predefined screen matched on a given day.",
        ),
        ApiExposure(
            name="screen_quotes",
            family=DataFamily.DISCOVERY,
            table="screen_quotes",
            sort_key=("as_of_date", "symbol"),
            descending=True,
            description="Quote snapshot captured with a screen run.",
        ),
        ApiExposure(
            name="screen_runs",
            family=DataFamily.DISCOVERY,
            table="screen_runs",
            sort_key=("as_of_date", "screen_key"),
            descending=True,
            filters=("screen_key",),
            description="When a screen last ran and how many it matched.",
        ),
    )

    gate_table = GATE_TABLE
    child_table = CHILD_TABLE
    gate_key_columns = GATE_KEY_COLUMNS

    # --- outer loop -----------------------------------------------------

    def variants(self, settings: Settings, state: VariantState | None) -> list[str]:
        """The screen set comes from `screens.py`; enabled state from the DB.

        Direction matters: the set comes from CODE, the DB only FILTERS.
        The reverse (set from DB) would deadlock bootstrap with an empty
        `screens` table before it's ever seeded -- since the table only
        fills during a run, the lock would never open.
        """
        cfg = settings
        wanted = comma_list(cfg.yf_screen_keys)
        keys = [s.key for s in ALL_SCREENS]
        if wanted:
            unknown = [k for k in wanted if k not in set(keys)]
            if unknown:
                raise ValueError(
                    f"unknown screen: {', '.join(unknown)}. "
                    f"valid names: {', '.join(sorted(keys))}"
                )
            keys = [k for k in keys if k in set(wanted)]
        # Read the disabled set ONCE. Asking per screen inside the
        # comprehension issued 19 round-trips for one answer.
        disabled = state.disabled_variants() if state is not None else frozenset()
        return [k for k in keys if k not in disabled]

    # --- fetch ------------------------------------------------------------

    def fetch(self, mctx: MarketContext) -> ScreenPayload:
        cfg = get_settings()
        if mctx.variant is None:  # pragma: no cover - defensive
            raise ValueError("screener cannot be called without a `variant`")
        spec = screen_by_key(mctx.variant)

        quotes: list[dict[str, Any]] = []
        metadata: dict[str, Any] = {}
        total = 0
        offset = 0
        pages = 0

        while pages < cfg.yf_screen_max_pages:
            page = _fetch_page(spec, offset=None if pages == 0 else offset, size=cfg.yf_screen_size)
            pages += 1
            if pages == 1:
                # The FIRST page is special: `title`, `description`,
                # `rawCriteria`, `lastUpdated` exist only in the predefined
                # GET response. For a custom screen, the first page is also
                # POST and metadata does NOT come back (measured) -- then
                # `ScreenDef` is the source of truth instead.
                metadata = page.metadata
                total = page.total
            quotes.extend(page.quotes)
            # THREE stop branches: empty page / offset >= total / page limit.
            # When `offset > total`, Yahoo returns 0 rows without erroring,
            # so the empty-page branch alone would suffice; the `total`
            # check just avoids one UNNECESSARY extra request.
            if not page.quotes:
                break
            offset += len(page.quotes)
            if offset >= page.total:
                break

        return ScreenPayload(
            screen_key=spec.key,
            as_of_date=utc_as_of_day(mctx.fetched_at),
            fetched_at=mctx.fetched_at,
            quotes=quotes,
            total=total,
            page_count=pages,
            metadata=metadata,
        )

    # --- normalize ----------------------------------------------------

    def normalize(self, raw: ScreenPayload) -> NormalizedResult:
        spec = screen_by_key(raw.screen_key)
        members: list[dict[str, Any]] = []
        quotes: list[dict[str, Any]] = []
        symbols: list[dict[str, Any]] = []
        seen: set[str] = set()

        for index, quote in enumerate(raw.quotes):
            symbol = nz.to_str(quote.get("symbol"))
            if symbol is None:
                # `screen` returned `symbol` on all 300 rows measured; still,
                # dropping the row beats writing NULL into the PK.
                continue
            is_known = symbol_is_writable(symbol)
            members.append(
                {
                    "screen_key": raw.screen_key,
                    "as_of_date": raw.as_of_date,
                    "symbol": symbol,
                    # `offset + in-page index` -> ABSOLUTE rank.
                    # `enumerate` gives this naturally because pages were
                    # appended IN ORDER.
                    "rank_index": index,
                    "is_known": is_known,
                    "fetched_at": raw.fetched_at,
                }
            )
            if symbol in seen:
                # If the same symbol appears on two pages (rank shift), the
                # quote is written once; the membership row is already
                # unique via its PK.
                continue
            seen.add(symbol)
            quotes.append(_quote_row(symbol, quote, raw, is_known=is_known))
            if is_known:
                symbols.append(_symbol_row(symbol, quote, raw))

        writes = [
            TableWrite(
                table="screens",
                rows=[_screen_row(spec, raw)],
                key_columns=("screen_key",),
                update_columns=_SCREEN_UPDATE,
            ),
            TableWrite(
                table="symbols",
                rows=symbols,
                key_columns=("symbol",),
                update_columns=SYMBOL_UPDATE,
            ),
            TableWrite(
                table="screen_quotes",
                rows=quotes,
                key_columns=("symbol", "as_of_date"),
                update_columns=_QUOTE_UPDATE,
            ),
            TableWrite(
                table=GATE_TABLE,
                rows=[_run_row(raw, members)],
                key_columns=GATE_KEY_COLUMNS,
                update_columns=_RUN_UPDATE,
            ),
            TableWrite(
                table=CHILD_TABLE,
                rows=members,
                key_columns=(*GATE_KEY_COLUMNS, "symbol"),
                update_columns=_MEMBER_UPDATE,
            ),
        ]
        return NormalizedResult(writes=writes)


def _screen_row(spec: ScreenDef, raw: ScreenPayload) -> dict[str, Any]:
    """`screens` row; if metadata exists, REFRESHED from the FIRST GET page."""
    meta = raw.metadata
    title = nz.to_str(meta.get("title")) or spec.title
    description = nz.to_str(meta.get("description")) or spec.description or None
    if spec.kind == "custom":
        definition = nz.canonical_json(spec.query.to_dict()) if spec.query else None
    else:
        definition = nz.canonical_json(meta.get("rawCriteria")) if meta.get("rawCriteria") else None
    return {
        "screen_key": spec.key,
        "kind": spec.kind,
        "quote_type": spec.quote_type,
        "title": title,
        "description": description,
        "sort_field": spec.sort_field,
        "sort_asc": spec.sort_asc,
        "definition_json": definition,
        "is_enabled": spec.is_enabled,
        "created_at": raw.fetched_at,
        "updated_at": raw.fetched_at,
    }


def _run_row(raw: ScreenPayload, members: list[dict[str, Any]]) -> dict[str, Any]:
    """Gate + data row.

    `content_hash` covers ONLY THE ROSTER: `(symbol, rank_index)` pairs. If
    quote metrics were included, `regularMarketPrice` moves on every run, so
    the hash would NEVER match, `skipped` would never occur, and the
    mechanism would silently die -- unnoticed, because the result would
    just look like "every row rewritten every day".

    `rank_index` IS in the body: when the roster stays the same but ORDER
    changes, that is a REAL change and must be written.
    """
    body = [{"symbol": m["symbol"], "rank_index": m["rank_index"]} for m in members]
    meta = raw.metadata
    return {
        "screen_key": raw.screen_key,
        "as_of_date": raw.as_of_date,
        "total": raw.total,
        "fetched_rows": len(raw.quotes),
        "row_count": len(members),
        "page_count": raw.page_count,
        "yahoo_id": nz.to_str(meta.get("id")),
        "version_id": nz.to_int(meta.get("versionId")),
        "last_updated": nz.epoch_to_datetime(meta.get("lastUpdated"), unit="ms"),
        "criteria_json": nz.canonical_json(meta.get("criteriaMeta"))
        if meta.get("criteriaMeta")
        else None,
        "content_hash": nz.content_hash(canonical=nz.canonical_json(body)),
        "fetched_at": raw.fetched_at,
    }


def _quote_row(
    symbol: str, quote: dict[str, Any], raw: ScreenPayload, *, is_known: bool
) -> dict[str, Any]:
    warn_unmapped(
        quote,
        SCREENER_QUOTE_FIELDS,
        dataset="screener",
        ignore=SCREENER_NON_COLUMN_SOURCES,
    )
    return {
        "symbol": symbol,
        "as_of_date": raw.as_of_date,
        **project_fields(quote, SCREENER_QUOTE_FIELDS),
        "is_known": is_known,
        # `corporateActions` IS A LIST and doesn't map to a column; it stays here.
        "raw_json": nz.canonical_json(quote),
        "fetched_at": raw.fetched_at,
    }


def _symbol_row(symbol: str, quote: dict[str, Any], raw: ScreenPayload) -> dict[str, Any]:
    """The screener quote is the WIDEST of the three discovery paths: it
    carries nine identifying fields, which is why `SYMBOL_UPDATE` is the
    widest one too.
    """
    return discovered_symbol_row(
        symbol,
        source="screener",
        fetched_at=raw.fetched_at,
        short_name=nz.to_str(quote.get("shortName"), max_len=128),
        long_name=nz.to_str(quote.get("longName"), max_len=255),
        exchange=nz.to_str(quote.get("exchange"), max_len=32),
        full_exchange_name=nz.to_str(quote.get("fullExchangeName"), max_len=64),
        quote_type=nz.to_str(quote.get("quoteType"), max_len=32),
        currency=nz.to_str(quote.get("currency"), max_len=32),
        timezone=nz.to_str(quote.get("exchangeTimezoneName"), max_len=64),
        first_trade_date=nz.epoch_to_datetime(
            quote.get("firstTradeDateMilliseconds"), unit="ms"
        ),
    )


# OPT-IN: `yfin screen sync` resolves this dataset BY NAME
# (`MARKET_DATASETS.resolve(["screener"])`), so the command works. A bare
# `yfin market sync` does NOT pull it in -- otherwise that command's cost
# would silently jump from ~20 requests to ~50, which nobody asked for.
register_market(ScreenerDataset(), opt_in=True)
