"""sector_rankings / industry_rankings -- as-of, BOLGELI (SI S7.4, S7.5).

Bolge YALNIZ bu liste bloklarini kapsiyor (olculdu): `topCompanies`,
`topETFs`, `topMutualFunds`, `topPerformingCompanies`, `topGrowthCompanies`.
`overview` / `performance` / `industries` / `researchReports` 5 bolgede
BIREBIR ayni dondu, bu yuzden onlar `*_profile` tarafinda ve bolgesiz.
"""

from __future__ import annotations

from typing import Any

from yfin.datasets.asof_base import DOMAIN_GATE_TABLE, asof_produces
from yfin.datasets.base import NormalizedResult, TableWrite, WriteStats
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
from yfin.logging_setup import get_logger
from yfin.persistence import RowWriter

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

    # --- ortak satir uretimi ---------------------------------------------

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
                # upsert sirasinda DB'den doldurulur
                "is_known": False,
                "fetched_at": raw.fetched_at,
            }
        return TableWrite(
            table=TOP_COMPANIES_TABLE,
            rows=list(rows.values()),
            key_columns=("domain_key", "region", "as_of_date", "symbol"),
            update_columns=(*COMPANY_COLUMNS, "fetched_at"),
            mode="replace_scope",
            # `domain_key` KAPSAMDA OLMAK ZORUNDA: bu tabloya IKI dataset
            # yaziyor. Kapsam yalniz (region, as_of_date) olsaydi
            # `industry_rankings` SEKTOR satirlarini silerdi (SI S5.11).
            scope_columns=("domain_key", "region", "as_of_date"),
            scope_values=(self._scope(raw, key),),
        )

    # --- yazma ------------------------------------------------------------

    def upsert(self, writer: RowWriter, result: NormalizedResult) -> WriteStats:
        """`is_known` DB'den doldurulur, SONRA as-of kapisi calisir.

        Sira baglayicidir ve bayrak hash GOVDESINE GIRER (`funds.py:432-437`
        bunu acikca gerekcelendiriyor): evren degistiginde -- kullanici
        `SGE.L`i `symbols`a ekledi -- kapi acilir ve satirlar guncellenir.
        Dislansaydi bayrak 0'da donup kalirdi, cunku kapi `skipped` derdi.
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
                # Ticker OLMAYABILIR: `0P0001WO1I` Morningstar kimligidir;
                # `is_known` bunu isaretler.
                symbol = text_of(entry, "symbol", 32)
                if symbol is None:
                    continue
                rows[(fund_type, symbol)] = {
                    **self._scope(raw, key),
                    "fund_type": fund_type,
                    "symbol": symbol,
                    # 220 fon satirinin 7'sinde YOK
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
        # `infrastructure-operations`: UC blogun hicbiri yok -> bos sonuc ->
        # `AsOfGate.upsert`'un `is_empty` dali kapi satirini YAZMAZ ve
        # hucreler `empty` olur (SI S7.5). Yahoo'nun bu endustride
        # `companiesCount=1` bildirmesi bunun hata degil olcum oldugunu
        # gosteriyor.
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
                    # `rank_type` PK'DADIR: tam-liste olcumunde 50 ortak
                    # sembolun 8'inde iki uc FARKLI `ytdReturn` bildiriyor.
                    "rank_type": rank_type,
                    "symbol": symbol,
                    "name": text_of(entry, "name", 255),
                    # ELOX 9999.0 -- SENTINEL SAYILMAZ
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
            # `rank_type` KAPSAMDA DEGIL: iki liste tek fetch'ten gelir ve
            # birlikte yazilir.
            scope_columns=("domain_key", "region", "as_of_date"),
            scope_values=(self._scope(raw, key),),
        )


def _mark_known(writer: RowWriter, write: TableWrite) -> TableWrite:
    """`symbol`de FK YOKTUR (SI S2): SGE.L, 285A.T, ODINE.IS, 0P0001WO1I
    evren disi. FK olsaydi tek yabanci sembol TURUN transaction'ini
    dusururdu -- `news_symbols` ve `fund_top_holdings` ile ayni gerekce."""
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
