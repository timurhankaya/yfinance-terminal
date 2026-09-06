"""sector_profile / industry_profile -- as-of, BOLGESIZ (SI S7.3).

`overview` + `performance` + `performanceOverviewBenchmark` -> 1
`domain_metrics` satiri; `researchReports[]` -> 4 `research_reports`
(upsert) + 4 `domain_report_links` satiri.

Bes blogun besi de US/GB/DE/JP/TR'de BIREBIR AYNI olculdu, bu yuzden bu
dataset'ler bolge dongusune GIRMEZ ve BIRINCIL bolgenin yanitini kullanir
(`ctx.cached` sayesinde ek HTTP istegi de uretmezler).
"""

from __future__ import annotations

from typing import Any

from yfin import normalize as nz
from yfin.datasets.asof_base import DOMAIN_GATE_TABLE, asof_produces
from yfin.datasets.base import NormalizedResult, TableWrite
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
from yfin.datasets.registry import register_domain
from yfin.logging_setup import get_logger

log = get_logger(__name__)

# `raw_json`a giren top-level anahtarlar: yanitin LISTE-DISI kismi.
# Liste bloklari DISARIDADIR (SI S2/S5.2): `nz.canonical_json` yalniz SOZLUK
# anahtarlarini siralar, LISTE SIRASINI KORUR (normalize.py:317-329). Tam
# zarf saklansaydi `topCompanies` sirasi (11 sektorun 8'inde 15 dk'da
# degisti) kapiyi HER KOSUDA acardi ve as-of mekanizmasi sessizce hic
# calismazdi.
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

# `performance` -> kolon; `performanceOverviewBenchmark` ayni anahtarlari
# `benchmark_` onekiyle tasir.
_PERFORMANCE_COLUMNS = {
    "ytdChangePercent": "ytd_change_pct",
    "regMarketChangePercent": "reg_market_change_pct",
    "oneYearChangePercent": "one_year_change_pct",
    "threeYearChangePercent": "three_year_change_pct",
    "fiveYearChangePercent": "five_year_change_pct",
}


class _DomainProfileDataset(DomainAsOfDataset[DomainPayload]):
    """Sektor ve endustri profilinin ortak govdesi."""

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

    # --- normalizasyon ----------------------------------------------------

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
        # `domains` EN SONA konur ve bunun FK ile ILGISI YOKTUR: `AsOfGate`
        # kapi satirinin `as_of_date`/`fetched_at`'ini `writes`'in ILK
        # satirindan okur ve `domains` satirinda `as_of_date` kolonu
        # YOKTUR -- basta olsaydi KeyError verirdi (SI S7.3).
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
            # ENDUSTRIDE ANAHTAR HAM JSON'DA HIC YOK -> None -> NULL
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
                # Kolon sinirsiz `text`tir: olculen max 23 570 karakter
                "report_title": text_of(report, "reportTitle"),
                # CIPLAK float gelir; 104 raporun 17'sinde ANAHTAR HIC YOK
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
            # `replace_scope` OLAMAZ: tablo PAYLASIMLIDIR (gunde 624 satirin
            # yalniz 516'si tekil; 37 tekil sektor raporunun HEPSI bir
            # endustride de goruluyor) ve kapsam kolonu yoktur.
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
        """Varsayilan: yazmaz. `industry_profile` bunu ezer."""
        return None


class SectorProfileDataset(_DomainProfileDataset):
    name = "sector_profile"
    depends_on = ("domain_taxonomy",)
    scope = "sector"
    # DORT tablo: sektorun `description`/`message_board_id` alanlarini zaten
    # bootstrap dolduruyor, cunku onlar sektor yanitinin `overview`
    # blogunda VAR.
    produces = asof_produces(
        METRICS_TABLE, REPORTS_TABLE, REPORT_LINKS_TABLE, gate=DOMAIN_GATE_TABLE
    )


class IndustryProfileDataset(_DomainProfileDataset):
    name = "industry_profile"
    depends_on = ("domain_taxonomy",)
    scope = "industry"
    # BES tablo: `domains` DAHIL (SI S8.3)
    produces = asof_produces(
        METRICS_TABLE,
        REPORTS_TABLE,
        REPORT_LINKS_TABLE,
        DOMAINS_TABLE,
        gate=DOMAIN_GATE_TABLE,
    )

    def _domains_write(self, raw: DomainPayload, key: str) -> TableWrite | None:
        """`description` + `message_board_id`; KIMLIK ALANLARINA DOKUNMAZ.

        Bu iki alan `industries[]` blogunda YOKTUR (SI S4.3), dolayisiyla
        bootstrap onlari endustriler icin dolduramaz. Kimlik alanlari
        (`symbol`, `parent_key`, `name`, `domain_type`) bootstrap'in isidir
        ve S5.11'deki AYRIK `update_columns` bunu zorlar -- satirda deger
        olarak bulunmalari yalnizca (hic olmamasi gereken) INSERT dali
        icindir.
        """
        overview = raw.data.get("overview") or {}
        parent = text_of(raw.data, "sectorKey", 48)
        if parent is not None and raw.expected_parent and parent != raw.expected_parent:
            # TAKSONOMI KAYMASI SINYALI. Satir `parent_key`i GUNCELLEMEZ
            # (update_columns disinda), bu yuzden sessiz bir ezme olmaz.
            log.warning(
                "sectorKey domains.parent_key ile uyusmuyor",
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
