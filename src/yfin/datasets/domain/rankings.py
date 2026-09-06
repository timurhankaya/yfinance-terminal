"""sector_rankings / industry_rankings -- as-of, regional.

Region only affects these list blocks (measured): `topCompanies`,
`topETFs`, `topMutualFunds`, `topPerformingCompanies`, `topGrowthCompanies`.
`overview` / `performance` / `industries` / `researchReports` were
byte-identical across all 5 regions, which is why they live on the
`*_profile` side and are region-less.
"""

from __future__ import annotations

from typing import Any

from yfin.core.logging_setup import get_logger
from yfin.datasets.asof_base import DOMAIN_GATE_TABLE, asof_produces
from yfin.datasets.base import NormalizedResult
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
from yfin.datasets.registry import register_domain
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

    def upsert(self, writer: RowWriter, result: NormalizedResult) -> WriteStats:
        """`is_known` is populated from the DB, then the as-of gate runs.

        Order matters, and the flag enters the hash body (mirrors the
        rationale in `funds.py`): when the universe changes -- a user adds
        `SGE.L` to `symbols` -- the gate reopens and rows update. Excluding
        it would leave the flag stuck at 0, since the gate would call it
        `skipped`.
        """
        writes = [
            _mark_known(writer, write)
            if write.rows and "is_known" in write.update_columns
            else write
            for write in result.writes
        ]
        return super().upsert(
            writer, NormalizedResult(writes=writes, skipped=dict(result.skipped))
        )


class SectorRankingsDataset(_DomainRankingsDataset):
    name = "sector_rankings"
    depends_on = ("domain_taxonomy",)
    scope = "sector"
    produces = asof_produces(TOP_COMPANIES_TABLE, TOP_FUNDS_TABLE, gate=DOMAIN_GATE_TABLE)

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
                    # Missing in 7 of 220 fund rows measured
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
    scope = "industry"
    produces = asof_produces(TOP_MOVERS_TABLE, TOP_COMPANIES_TABLE, gate=DOMAIN_GATE_TABLE)

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
                    # `rank_type` is in the PK: full-list measurement found
                    # 8 of 50 shared symbols report two different
                    # `ytdReturn` values across the two blocks.
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


def _mark_known(writer: RowWriter, write: TableWrite) -> TableWrite:
    """No FK on `symbol`: SGE.L, 285A.T, ODINE.IS, 0P0001WO1I are outside
    the universe. An FK would drop the whole pass's transaction over one
    foreign symbol -- same rationale as `news_symbols` and `fund_top_holdings`."""
    candidates = {row["symbol"] for row in write.rows}
    known = writer.known_symbols(candidates)
    rows = [{**row, "is_known": row["symbol"] in known} for row in write.rows]
    return TableWrite(
        table=write.table,
        rows=rows,
        key_columns=write.key_columns,
        update_columns=write.update_columns,
        mode=write.mode,
        scope_columns=write.scope_columns,
        scope_values=write.scope_values,
    )


register_domain(SectorRankingsDataset())
register_domain(IndustryRankingsDataset())
