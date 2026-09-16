"""sector_rankings / industry_rankings -- as-of, regional.

Only the `top*` list blocks vary by region; the rest lives on the
region-less `*_profile` side.
"""

from __future__ import annotations

from typing import Any

from yfin.core.families import DataFamily
from yfin.core.logging_setup import get_logger
from yfin.datasets.asof_base import DOMAIN_GATE_TABLE, asof_produces
from yfin.datasets.base import NormalizedResult, mark_known_in
from yfin.datasets.domain.base import DomainAsOfDataset, DomainContext
from yfin.datasets.domain.common import (
    MAPPED_KEYS,
    TOP_COMPANIES_TABLE,
    TOP_FUNDS_TABLE,
    TOP_MOVERS_TABLE,
    big_of,
    dec_of,
    fetch_domain,
    text_of,
    warn_unmapped,
)
from yfin.datasets.domain.payloads import DomainPayload
from yfin.datasets.exposure import ApiExposure
from yfin.datasets.registry import register_domain
from yfin.models.domains import DomainType
from yfin.storage.contracts import RowWriter, TableWrite, WriteStats

log = get_logger(__name__)

COMPANY_COLUMNS = (
    "name",
    "rating",
    "market_weight",
    "market_cap",
    "last_price",
    "target_price",
    "ytd_return",
    "reg_market_change_pct",
    "is_known",
)
FUND_COLUMNS = ("name", "net_assets", "expense_ratio", "last_price", "ytd_return", "is_known")
MOVER_COLUMNS = (
    "name",
    "ytd_return",
    "last_price",
    "target_price",
    "growth_estimate",
    "is_known",
)


class _DomainRankingsDataset(DomainAsOfDataset[DomainPayload]):
    regional = True

    def fetch(self, ctx: DomainContext) -> DomainPayload:
        key = ctx.target_key
        region = ctx.fetch_region
        data = ctx.cached(
            f"raw:{key}:{region}", lambda: fetch_domain(key, ctx.target_type, region)
        )
        return DomainPayload(
            data=data,
            fetched_at=ctx.fetched_at,
            as_of_date=ctx.as_of_date,
            region=region,
            domain_type=ctx.target_type,
        )

    # --- shared row generation ---------------------------------------------

    def _scope(self, raw: DomainPayload, key: str) -> dict[str, Any]:
        return {"domain_key": key, "region": raw.region, "as_of_date": raw.as_of_date}

    def _companies_write(self, raw: DomainPayload, key: str) -> TableWrite:
        block = raw.data.get("topCompanies") or []
        warn_unmapped(self.name, key, "topCompanies", block, MAPPED_KEYS["topCompanies"])
        rows: dict[str, dict[str, Any]] = {}
        for entry in block:
            if not isinstance(entry, dict):
                continue
            symbol = text_of(entry, "symbol", 32)
            if symbol is None:
                continue
            rows[symbol] = {
                **self._scope(raw, key),
                "symbol": symbol,
                "name": text_of(entry, "name", 255),
                "rating": text_of(entry, "rating", 32),
                "market_weight": dec_of(entry, "marketWeight"),
                "market_cap": big_of(entry, "marketCap"),
                "last_price": dec_of(entry, "lastPrice"),
                "target_price": dec_of(entry, "targetPrice"),
                "ytd_return": dec_of(entry, "ytdReturn"),
                "reg_market_change_pct": dec_of(entry, "regMarketChangePercent"),
                # Populated from the DB during upsert
                "is_known": False,
                "fetched_at": raw.fetched_at,
            }
        return TableWrite(
            table=TOP_COMPANIES_TABLE,
            rows=list(rows.values()),
            key_columns=("domain_key", "region", "as_of_date", "symbol"),
            update_columns=(*COMPANY_COLUMNS, "fetched_at"),
            mode="replace_scope",
            # `domain_key` must be in scope: two datasets write to this
            # table. If scope were just (region, as_of_date),
            # `industry_rankings` would delete sector rows.
            scope_columns=("domain_key", "region", "as_of_date"),
            scope_values=(self._scope(raw, key),),
        )

    # --- write ------------------------------------------------------------

    def upsert(
        self, writer: RowWriter, result: NormalizedResult, *, full_refresh: bool = False
    ) -> WriteStats:
        """`is_known` is populated from the DB, then the as-of gate runs.

        The flag enters the hash body so a universe change reopens the gate.
        """
        # No FK on `symbol`: one foreign symbol would drop the whole pass.
        marked = mark_known_in(
            writer,
            result,
            select=lambda write: bool(write.rows) and "is_known" in write.update_columns,
        )
        return super().upsert(writer, marked, full_refresh=full_refresh)


class SectorRankingsDataset(_DomainRankingsDataset):
    name = "sector_rankings"
    depends_on = ("domain_taxonomy",)
    scope = DomainType.SECTOR
    produces = asof_produces(TOP_COMPANIES_TABLE, TOP_FUNDS_TABLE, gate=DOMAIN_GATE_TABLE)
    # Either block can come back empty on its own; the pair cannot, or
    # the result is empty and no gate row is written at all.
    gate_source_tables = (TOP_COMPANIES_TABLE, TOP_FUNDS_TABLE)
    api = (
        ApiExposure(
            name="domain_top_companies",
            family=DataFamily.DOMAINS,
            table="domain_top_companies",
            sort_key=("as_of_date", "domain_key", "region", "symbol"),
            descending=True,
            filters=("domain_key", "region"),
            symbol_optional=True,
            description="Largest companies in a sector or industry.",
        ),
        ApiExposure(
            name="domain_top_funds",
            family=DataFamily.DOMAINS,
            table="domain_top_funds",
            sort_key=("as_of_date", "domain_key", "region", "fund_type", "symbol"),
            descending=True,
            filters=("domain_key", "region", "fund_type"),
            symbol_optional=True,
            description="Largest funds tracking a sector or industry.",
        ),
    )

    def normalize(self, raw: DomainPayload, key: str) -> NormalizedResult:
        return NormalizedResult(
            writes=[self._companies_write(raw, key), self._funds_write(raw, key)]
        )

    def _funds_write(self, raw: DomainPayload, key: str) -> TableWrite:
        rows: dict[tuple[str, str], dict[str, Any]] = {}
        for fund_type, block_name in (("etf", "topETFs"), ("mutual_fund", "topMutualFunds")):
            block = raw.data.get(block_name) or []
            warn_unmapped(self.name, key, block_name, block, MAPPED_KEYS[block_name])
            for entry in block:
                if not isinstance(entry, dict):
                    continue
                # May not be a ticker: `0P0001WO1I` is a Morningstar id;
                # `is_known` flags this.
                symbol = text_of(entry, "symbol", 32)
                if symbol is None:
                    continue
                rows[(fund_type, symbol)] = {
                    **self._scope(raw, key),
                    "fund_type": fund_type,
                    "symbol": symbol,
                    # Can be missing
                    "name": text_of(entry, "name", 255),
                    "net_assets": big_of(entry, "netAssets"),
                    "expense_ratio": dec_of(entry, "expenseRatio"),
                    "last_price": dec_of(entry, "lastPrice"),
                    "ytd_return": dec_of(entry, "ytdReturn"),
                    "is_known": False,
                    "fetched_at": raw.fetched_at,
                }
        return TableWrite(
            table=TOP_FUNDS_TABLE,
            rows=list(rows.values()),
            key_columns=("domain_key", "region", "as_of_date", "fund_type", "symbol"),
            update_columns=(*FUND_COLUMNS, "fetched_at"),
            mode="replace_scope",
            scope_columns=("domain_key", "region", "as_of_date"),
            scope_values=(self._scope(raw, key),),
        )


class IndustryRankingsDataset(_DomainRankingsDataset):
    name = "industry_rankings"
    depends_on = ("domain_taxonomy",)
    scope = DomainType.INDUSTRY
    produces = asof_produces(TOP_MOVERS_TABLE, TOP_COMPANIES_TABLE, gate=DOMAIN_GATE_TABLE)
    gate_source_tables = (TOP_MOVERS_TABLE, TOP_COMPANIES_TABLE)
    api = (
        ApiExposure(
            name="domain_top_movers",
            family=DataFamily.DOMAINS,
            table="domain_top_movers",
            sort_key=("as_of_date", "domain_key", "region", "rank_type", "symbol"),
            descending=True,
            filters=("domain_key", "region", "rank_type"),
            symbol_optional=True,
            description="Gainers, losers and most active names in a domain.",
        ),
    )

    def normalize(self, raw: DomainPayload, key: str) -> NormalizedResult:
        # `infrastructure-operations`: none of the three blocks are
        # present -> empty result -> `AsOfGate.upsert`'s `is_empty` branch
        # writes no gate row, and cells come out `empty`. Yahoo reporting
        # `companiesCount=1` for this industry confirms this is real, not a bug.
        return NormalizedResult(
            writes=[self._movers_write(raw, key), self._companies_write(raw, key)]
        )

    def _movers_write(self, raw: DomainPayload, key: str) -> TableWrite:
        rows: dict[tuple[str, str], dict[str, Any]] = {}
        blocks = (
            ("performing", "topPerformingCompanies"),
            ("growth", "topGrowthCompanies"),
        )
        for rank_type, block_name in blocks:
            block = raw.data.get(block_name) or []
            warn_unmapped(self.name, key, block_name, block, MAPPED_KEYS[block_name])
            for entry in block:
                if not isinstance(entry, dict):
                    continue
                symbol = text_of(entry, "symbol", 32)
                if symbol is None:
                    continue
                rows[(rank_type, symbol)] = {
                    **self._scope(raw, key),
                    # `rank_type` is in the PK: a symbol shared by both blocks
                    # can carry a different `ytdReturn` in each.
                    "rank_type": rank_type,
                    "symbol": symbol,
                    "name": text_of(entry, "name", 255),
                    # ELOX reports 9999.0 -- not treated as a sentinel
                    "ytd_return": dec_of(entry, "ytdReturn"),
                    "last_price": dec_of(entry, "lastPrice"),
                    "target_price": dec_of(entry, "targetPrice"),
                    "growth_estimate": dec_of(entry, "growthEstimate"),
                    "is_known": False,
                    "fetched_at": raw.fetched_at,
                }
        return TableWrite(
            table=TOP_MOVERS_TABLE,
            rows=list(rows.values()),
            key_columns=("domain_key", "region", "as_of_date", "rank_type", "symbol"),
            update_columns=(*MOVER_COLUMNS, "fetched_at"),
            mode="replace_scope",
            # `rank_type` is not in scope: both lists come from a single
            # fetch and are written together.
            scope_columns=("domain_key", "region", "as_of_date"),
            scope_values=(self._scope(raw, key),),
        )


register_domain(SectorRankingsDataset())
register_domain(IndustryRankingsDataset())
