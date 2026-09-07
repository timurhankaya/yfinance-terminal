"""sector_profile / industry_profile -- as-of, region-less.

`overview` + `performance` + `performanceOverviewBenchmark` -> 1
`domain_metrics` row; `researchReports[]` -> 4 `research_reports`
(upsert) + 4 `domain_report_links` rows.

All five blocks were measured byte-identical across US/GB/DE/JP/TR, so
these datasets skip the region loop and use the primary region's response
(and, thanks to `ctx.cached`, produce no extra HTTP request).
"""

from __future__ import annotations

from typing import Any

from yfin.core import normalize as nz
from yfin.core.families import DataFamily
from yfin.core.logging_setup import get_logger
from yfin.datasets.asof_base import DOMAIN_GATE_TABLE, asof_produces
from yfin.datasets.base import NormalizedResult
from yfin.datasets.domain.base import DomainAsOfDataset, DomainContext
from yfin.datasets.domain.common import (
    DOMAINS_TABLE,
    MAPPED_KEYS,
    METRICS_TABLE,
    REPORT_LINKS_TABLE,
    REPORTS_TABLE,
    big_of,
    dec_of,
    fetch_domain,
    int_of,
    text_of,
    ubig_of,
    warn_unmapped,
)
from yfin.datasets.domain.payloads import DomainPayload
from yfin.datasets.exposure import ApiExposure
from yfin.datasets.registry import register_domain
from yfin.storage.contracts import TableWrite

log = get_logger(__name__)

# Top-level keys stored in `raw_json`: the non-list part of the response.
# List blocks are excluded: `nz.canonical_json` only sorts dict keys and
# preserves list order. Storing the full envelope would let
# `topCompanies`'s order (observed to change within 15 minutes for 8 of 11
# sectors) reopen the gate on every run, silently disabling the as-of
# mechanism.
RAW_JSON_KEYS = (
    "key",
    "name",
    "symbol",
    "sectorKey",
    "sectorName",
    "overview",
    "performance",
    "performanceOverviewBenchmark",
)

METRIC_COLUMNS = (
    "companies_count",
    "industries_count",
    "market_cap",
    "market_weight",
    "employee_count",
    "ytd_change_pct",
    "reg_market_change_pct",
    "one_year_change_pct",
    "three_year_change_pct",
    "five_year_change_pct",
    "benchmark_name",
    "benchmark_ytd_change_pct",
    "benchmark_reg_market_change_pct",
    "benchmark_one_year_change_pct",
    "benchmark_three_year_change_pct",
    "benchmark_five_year_change_pct",
    "raw_json",
)

REPORT_COLUMNS = (
    "provider",
    "report_type",
    "head_html",
    "report_title",
    "target_price",
    "target_price_status",
    "investment_rating",
    "report_ts_utc",
    "as_of_date",
)

# `performance` -> column; `performanceOverviewBenchmark` carries the same
# keys with a `benchmark_` prefix.
_PERFORMANCE_COLUMNS = {
    "ytdChangePercent": "ytd_change_pct",
    "regMarketChangePercent": "reg_market_change_pct",
    "oneYearChangePercent": "one_year_change_pct",
    "threeYearChangePercent": "three_year_change_pct",
    "fiveYearChangePercent": "five_year_change_pct",
}


class _DomainProfileDataset(DomainAsOfDataset[DomainPayload]):
    """Shared body for sector and industry profile."""

    regional = False

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
            region=ctx.region,
            domain_type=ctx.target_type,
            expected_parent=ctx.parents.get(key),
        )

    # --- normalize ----------------------------------------------------

    def normalize(self, raw: DomainPayload, key: str) -> NormalizedResult:
        data = raw.data
        overview = data.get("overview") or {}
        performance = data.get("performance") or {}
        benchmark = data.get("performanceOverviewBenchmark") or {}

        warn_unmapped(self.name, key, "topLevel", data, MAPPED_KEYS["topLevel"])
        warn_unmapped(
            self.name,
            key,
            "overview",
            overview,
            _overview_keys(raw.domain_type),
        )
        warn_unmapped(self.name, key, "performance", performance, MAPPED_KEYS["performance"])
        warn_unmapped(
            self.name,
            key,
            "performanceOverviewBenchmark",
            benchmark,
            MAPPED_KEYS["performanceOverviewBenchmark"],
        )

        writes = [
            self._metrics_write(raw, key, overview, performance, benchmark),
            *self._report_writes(raw, key),
        ]
        # `domains` is placed last, and this has nothing to do with FKs:
        # `AsOfGate` reads the gate row's `as_of_date`/`fetched_at` from the
        # first row in `writes`, and the `domains` row has no `as_of_date`
        # column -- placing it first would raise KeyError.
        extra = self._domains_write(raw, key)
        if extra is not None:
            writes.append(extra)
        return NormalizedResult(writes=writes)

    def _metrics_write(
        self,
        raw: DomainPayload,
        key: str,
        overview: dict[str, Any],
        performance: dict[str, Any],
        benchmark: dict[str, Any],
    ) -> TableWrite:
        row: dict[str, Any] = {
            "domain_key": key,
            "as_of_date": raw.as_of_date,
            "companies_count": int_of(overview, "companiesCount"),
            # Key is entirely absent from the raw JSON for an industry -> None -> NULL
            "industries_count": int_of(overview, "industriesCount"),
            "market_cap": big_of(overview, "marketCap"),
            "market_weight": dec_of(overview, "marketWeight"),
            "employee_count": ubig_of(overview, "employeeCount"),
            "benchmark_name": text_of(benchmark, "name", 64),
            "raw_json": nz.canonical_json(
                {name: data for name in RAW_JSON_KEYS if (data := raw.data.get(name)) is not None}
            ),
            "fetched_at": raw.fetched_at,
        }
        for source, column in _PERFORMANCE_COLUMNS.items():
            row[column] = dec_of(performance, source)
            row[f"benchmark_{column}"] = dec_of(benchmark, source)
        return TableWrite(
            table=METRICS_TABLE,
            rows=[row],
            key_columns=("domain_key", "as_of_date"),
            update_columns=(*METRIC_COLUMNS, "fetched_at"),
            mode="replace_scope",
            scope_columns=("domain_key", "as_of_date"),
        )

    def _report_writes(self, raw: DomainPayload, key: str) -> list[TableWrite]:
        reports = raw.data.get("researchReports") or []
        warn_unmapped(self.name, key, "researchReports", reports, MAPPED_KEYS["researchReports"])

        report_rows: dict[str, dict[str, Any]] = {}
        link_rows: list[dict[str, Any]] = []
        for position, report in enumerate(reports):
            if not isinstance(report, dict):
                continue
            report_id = text_of(report, "id", 64)
            if report_id is None or report_id in report_rows:
                continue
            report_rows[report_id] = {
                "report_id": report_id,
                "as_of_date": raw.as_of_date,
                "provider": text_of(report, "provider", 64),
                "report_type": text_of(report, "reportType", 64),
                "head_html": text_of(report, "headHtml", 255),
                # Column is unbounded `text`: measured max 23,570 characters
                "report_title": text_of(report, "reportTitle"),
                # Arrives as a bare float; missing entirely in 17 of 104 reports measured
                "target_price": dec_of(report, "targetPrice"),
                "target_price_status": text_of(report, "targetPriceStatus", 32),
                "investment_rating": text_of(report, "investmentRating", 32),
                "report_ts_utc": nz.to_datetime_utc(report.get("reportDate")),
                "first_seen_at": raw.fetched_at,
                "fetched_at": raw.fetched_at,
            }
            link_rows.append(
                {
                    "domain_key": key,
                    "as_of_date": raw.as_of_date,
                    "report_id": report_id,
                    "position": position,
                    "fetched_at": raw.fetched_at,
                }
            )

        return [
            # Cannot use `replace_scope`: the table is shared (only 516 of
            # 624 daily rows measured were unique; all 37 unique sector
            # reports also appear under an industry) and has no scope column.
            TableWrite(
                table=REPORTS_TABLE,
                rows=list(report_rows.values()),
                key_columns=("report_id",),
                update_columns=(*REPORT_COLUMNS, "fetched_at"),
            ),
            TableWrite(
                table=REPORT_LINKS_TABLE,
                rows=link_rows,
                key_columns=("domain_key", "as_of_date", "report_id"),
                update_columns=("position", "fetched_at"),
                mode="replace_scope",
                scope_columns=("domain_key", "as_of_date"),
            ),
        ]

    def _domains_write(self, raw: DomainPayload, key: str) -> TableWrite | None:
        """Default: writes nothing. `industry_profile` overrides this."""
        return None


class SectorProfileDataset(_DomainProfileDataset):
    name = "sector_profile"
    depends_on = ("domain_taxonomy",)
    scope = "sector"
    # Four tables: bootstrap already populates the sector's
    # `description`/`message_board_id` fields, since they exist in the
    # sector response's `overview` block.
    produces = asof_produces(
        METRICS_TABLE, REPORTS_TABLE, REPORT_LINKS_TABLE, gate=DOMAIN_GATE_TABLE
    )
    api = (
        ApiExposure(
            name="domain_metrics",
            family=DataFamily.DOMAINS,
            table="domain_metrics",
            sort_key=("as_of_date", "domain_key"),
            descending=True,
            filters=("domain_key",),
            description="Company and employee counts, market weight per domain.",
        ),
        ApiExposure(
            name="research_reports",
            family=DataFamily.DOMAINS,
            table="research_reports",
            sort_key=("as_of_date", "report_id"),
            descending=True,
            description="Research reports linked to sectors and industries.",
        ),
    )


class IndustryProfileDataset(_DomainProfileDataset):
    name = "industry_profile"
    depends_on = ("domain_taxonomy",)
    scope = "industry"
    # Five tables: includes `domains`
    produces = asof_produces(
        METRICS_TABLE,
        REPORTS_TABLE,
        REPORT_LINKS_TABLE,
        DOMAINS_TABLE,
        gate=DOMAIN_GATE_TABLE,
    )

    def _domains_write(self, raw: DomainPayload, key: str) -> TableWrite | None:
        """`description` + `message_board_id`; does not touch identity fields.

        These two fields are absent from the `industries[]` block, so
        bootstrap cannot populate them for industries. Identity fields
        (`symbol`, `parent_key`, `name`, `domain_type`) are bootstrap's job,
        enforced by the separate `update_columns` here -- their presence as
        values in the row only matters for the INSERT branch, which should
        never actually fire.
        """
        overview = raw.data.get("overview") or {}
        parent = text_of(raw.data, "sectorKey", 48)
        if parent is not None and raw.expected_parent and parent != raw.expected_parent:
            # Signal of taxonomy drift. The row does not update `parent_key`
            # (outside update_columns), so this cannot silently overwrite it.
            log.warning(
                "sectorKey does not match domains.parent_key",
                dataset=self.name,
                domain_key=key,
                response_parent=parent,
                db_parent=raw.expected_parent,
            )
        row = {
            "domain_key": key,
            "domain_type": "industry",
            "symbol": text_of(raw.data, "symbol", 32),
            "parent_key": parent or raw.expected_parent,
            "name": text_of(raw.data, "name", 64),
            "description": text_of(overview, "description"),
            "message_board_id": text_of(overview, "messageBoardId", 32),
            "first_seen_at": raw.fetched_at,
            "fetched_at": raw.fetched_at,
        }
        return TableWrite(
            table=DOMAINS_TABLE,
            rows=[row],
            key_columns=("domain_key",),
            update_columns=("description", "message_board_id", "fetched_at"),
        )


def _overview_keys(domain_type: str) -> frozenset[str]:
    from yfin.datasets.domain.common import _MAPPED_OVERVIEW_KEYS

    return _MAPPED_OVERVIEW_KEYS[domain_type]


register_domain(SectorProfileDataset())
register_domain(IndustryProfileDataset())
