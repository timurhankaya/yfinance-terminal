"""domain_taxonomy -- bootstrap dataset.

Region-less, plain upsert, single pass (`per_key = False`): the taxonomy
is written in one transaction, consistent as a whole or not at all.
"""

from __future__ import annotations

from functools import partial
from typing import Any

from yfin.core.families import DataFamily
from yfin.core.logging_setup import get_logger
from yfin.datasets.base import NormalizedResult
from yfin.datasets.domain.base import DomainContext, DomainDataset
from yfin.datasets.domain.common import (
    DOMAINS_TABLE,
    SECTOR_KEYS,
    SYMBOLS_TABLE,
    fetch_domain,
    text_of,
)
from yfin.datasets.domain.payloads import TaxonomyPayload
from yfin.datasets.exposure import ApiExposure
from yfin.datasets.registry import register_domain
from yfin.models.domains import DomainType
from yfin.storage.contracts import TableWrite

log = get_logger(__name__)

# Identical for every domain symbol. Currency and timezone are absent
# from the sector/industry response and stay NULL.
DOMAIN_QUOTE_TYPE = "INDEX"
DOMAIN_EXCHANGE = "YHD"

# `is_active` and `unknown_streak` are outside update_columns: if a user
# manually activates `^YH311`, the next domain sync will not deactivate it
# again.
SYMBOL_UPDATE_COLUMNS = (
    "short_name",
    "quote_type",
    "exchange",
    "currency",
    "timezone",
    "last_seen_at",
)
# `first_seen_at` is excluded. `description` / `message_board_id` are also
# excluded: on the industry side `industry_profile` writes them, and having
# the two writers update disjoint column sets keeps them from overwriting
# each other.
DOMAIN_UPDATE_COLUMNS = ("symbol", "parent_key", "name", "fetched_at")


class DomainTaxonomyDataset(DomainDataset[TaxonomyPayload]):
    name = "domain_taxonomy"
    scope = DomainType.SECTOR
    regional = False
    per_key = False
    produces = (SYMBOLS_TABLE, DOMAINS_TABLE)
    api = (
        ApiExposure(
            name="domains",
            family=DataFamily.DOMAINS,
            table="domains",
            sort_key=("domain_key",),
            filters=("domain_type", "parent_key"),
            description="The sector and industry taxonomy.",
        ),
    )

    def fetch(self, ctx: DomainContext) -> TaxonomyPayload:
        region = ctx.fetch_region
        sectors: dict[str, dict[str, Any]] = {}
        for key in SECTOR_KEYS:
            # `partial`: a lambda default would capture the loop variable.
            sectors[key] = ctx.cached(
                f"raw:{key}:{region}", partial(fetch_domain, key, "sector", region)
            )
        return TaxonomyPayload(sectors=sectors, fetched_at=ctx.fetched_at)

    def normalize(self, raw: TaxonomyPayload, key: str) -> NormalizedResult:
        fetched_at = raw.fetched_at
        symbol_rows: list[dict[str, Any]] = []
        domain_rows: list[dict[str, Any]] = []

        for sector_key in SECTOR_KEYS:
            data = raw.sectors.get(sector_key)
            if not data:
                continue
            overview = data.get("overview") or {}
            sector_symbol = text_of(data, "symbol", 32)
            sector_name = text_of(data, "name", 64)
            if sector_symbol is None or sector_name is None:
                # `symbol` is UNIQUE NOT NULL, `name` is NOT NULL: if missing,
                # the row hits a NOT NULL violation (23502) and drops the whole pass.
                log.debug("the sector identity field is missing", domain_key=sector_key)
                continue

            symbol_rows.append(_symbol_row(sector_symbol, sector_name, fetched_at))
            domain_rows.append(
                {
                    "domain_key": sector_key,
                    "domain_type": "sector",
                    "symbol": sector_symbol,
                    "parent_key": None,
                    "name": sector_name,
                    "description": text_of(overview, "description"),
                    "message_board_id": text_of(overview, "messageBoardId", 32),
                    "first_seen_at": fetched_at,
                    "fetched_at": fetched_at,
                }
            )

            # Row order within a single `TableWrite` matters: `parent_key`
            # is a self-FK, so the sector's row must come before its own
            # industries. `apply_write` sends rows in the order given.
            for row in data.get("industries") or []:
                # The "All Industries" aggregate row has no `key`; filtering
                # on that avoids yfinance's language-dependent name check.
                industry_key = text_of(row, "key", 48)
                if industry_key is None:
                    continue
                industry_symbol = text_of(row, "symbol", 32)
                industry_name = text_of(row, "name", 64)
                if industry_symbol is None or industry_name is None:
                    log.debug("the industry identity field is missing", domain_key=industry_key)
                    continue
                symbol_rows.append(_symbol_row(industry_symbol, industry_name, fetched_at))
                domain_rows.append(
                    {
                        "domain_key": industry_key,
                        "domain_type": "industry",
                        "symbol": industry_symbol,
                        "parent_key": sector_key,
                        "name": industry_name,
                        # These two fields are absent from the
                        # `industries[]` block; `industry_profile` fills
                        # them in for an industry.
                        "description": None,
                        "message_board_id": None,
                        "first_seen_at": fetched_at,
                        "fetched_at": fetched_at,
                    }
                )

        return NormalizedResult(
            writes=[
                # Table order matters: writing `symbols` must precede
                # `domains.symbol`'s FK.
                TableWrite(
                    table=SYMBOLS_TABLE,
                    rows=symbol_rows,
                    key_columns=("symbol",),
                    update_columns=SYMBOL_UPDATE_COLUMNS,
                ),
                TableWrite(
                    table=DOMAINS_TABLE,
                    rows=domain_rows,
                    key_columns=("domain_key",),
                    update_columns=DOMAIN_UPDATE_COLUMNS,
                ),
            ]
        )


def _symbol_row(symbol: str, name: str, fetched_at: Any) -> dict[str, Any]:
    """`symbols` row; `is_active` is explicitly set to 0 (server_default is '1').

    Sector/industry indices stay out of the default `yfin sync` universe;
    `--include-inactive` or `--quote-type INDEX` still reaches them.
    """
    return {
        "symbol": symbol,
        "short_name": name[:128],
        "quote_type": DOMAIN_QUOTE_TYPE,
        "exchange": DOMAIN_EXCHANGE,
        "currency": None,
        "timezone": None,
        "is_active": False,
        "last_seen_at": fetched_at,
    }


register_domain(DomainTaxonomyDataset())
