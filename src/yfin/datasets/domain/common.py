"""Shared pieces for domain datasets.

The data source is the raw JSON, fetched through yfinance's own `YfData`
layer: the `Sector`/`Industry` classes discard whole blocks of it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from yfin.core import normalize as nz
from yfin.core.logging_setup import get_logger
from yfin.ingest.client import call_yahoo

log = get_logger(__name__)

_QUERY = "https://query1.finance.yahoo.com/v1/finance"

# --- table names ----------------------------------------------------------

DOMAINS_TABLE = "domains"
SYMBOLS_TABLE = "symbols"
METRICS_TABLE = "domain_metrics"
TOP_COMPANIES_TABLE = "domain_top_companies"
TOP_FUNDS_TABLE = "domain_top_funds"
TOP_MOVERS_TABLE = "domain_top_movers"
REPORTS_TABLE = "research_reports"
REPORT_LINKS_TABLE = "domain_report_links"

# --- key universe --------------------------------------------------------

# Yahoo has no "list sectors" endpoint. Industry keys are never taken from
# yfinance's `SECTOR_INDUSTY_MAPPING_LC` (many of them 404 live); they are
# discovered from the sector response's `industries[].key`.
SECTOR_KEYS: tuple[str, ...] = (
    "basic-materials",
    "communication-services",
    "consumer-cyclical",
    "consumer-defensive",
    "energy",
    "financial-services",
    "healthcare",
    "industrials",
    "real-estate",
    "technology",
    "utilities",
)

# --- as-of day ------------------------------------------------------------

# Domain data is a US market aggregate. Using the UTC day would produce two
# rows for the same trading day from runs either side of midnight UTC.
MARKET_TZ = ZoneInfo("America/New_York")


def as_of_day(fetched_at: datetime) -> date:
    moment = fetched_at if fetched_at.tzinfo is not None else fetched_at.replace(tzinfo=UTC)
    return moment.astimezone(MARKET_TZ).date()


# --- envelope unwrapping ---------------------------------------------------


def unwrap(value: Any) -> Any:
    """Unwraps Yahoo's {"raw":..., "fmt":...} envelope.

    The same field arrives wrapped in one block and bare in another.
    """
    if isinstance(value, Mapping):
        return value.get("raw")
    return value


def _text(value: Any, max_len: int | None) -> str | None:
    """Text field; a blank string is converted to NULL (same rule as AH's priceTargetAction)."""
    text = nz.to_str(unwrap(value), max_len=max_len)
    if text is None:
        return None
    stripped = text.strip()
    return stripped or None


def text_of(row: Mapping[str, Any], key: str, max_len: int | None = None) -> str | None:
    return _text(row.get(key), max_len)


def dec_of(row: Mapping[str, Any], key: str) -> Any:
    """DECIMAL(28,12) field. Missing key -> None -> NULL."""
    return nz.to_decimal(unwrap(row.get(key)))


def int_of(row: Mapping[str, Any], key: str) -> int | None:
    return nz.to_int(unwrap(row.get(key)))


def big_of(row: Mapping[str, Any], key: str) -> Any:
    from yfin.models.kinds import KINDS

    return KINDS["big"].convert(unwrap(row.get(key)))


def ubig_of(row: Mapping[str, Any], key: str) -> Any:
    from yfin.models.kinds import KINDS

    return KINDS["ubig"].convert(unwrap(row.get(key)))


# --- mapped key sets -----------------------------------------------------

# `overview` has two variants: `industriesCount` exists only for a sector.
# A single shared set would warn on every industry or every sector.
_MAPPED_OVERVIEW_KEYS: dict[str, frozenset[str]] = {
    "sector": frozenset(
        {
            "companiesCount",
            "marketCap",
            "messageBoardId",
            "description",
            "industriesCount",
            "employeeCount",
            "marketWeight",
        }
    ),
    "industry": frozenset(
        {
            "companiesCount",
            "marketCap",
            "messageBoardId",
            "description",
            "employeeCount",
            "marketWeight",
        }
    ),
}

_MAPPED_PERFORMANCE_KEYS = frozenset(
    {
        "ytdChangePercent",
        "regMarketChangePercent",
        "oneYearChangePercent",
        "threeYearChangePercent",
        "fiveYearChangePercent",
    }
)
_MAPPED_BENCHMARK_KEYS = _MAPPED_PERFORMANCE_KEYS | {"name"}

_MAPPED_COMPANY_KEYS = frozenset(
    {
        "symbol",
        "name",
        "rating",
        "marketWeight",
        "marketCap",
        "targetPrice",
        "lastPrice",
        "ytdReturn",
        "regMarketChangePercent",
    }
)
_MAPPED_FUND_KEYS = frozenset(
    {"symbol", "name", "netAssets", "expenseRatio", "lastPrice", "ytdReturn"}
)
_MAPPED_MOVER_KEYS = frozenset(
    {"symbol", "name", "ytdReturn", "lastPrice", "targetPrice", "growthEstimate"}
)
_MAPPED_REPORT_KEYS = frozenset(
    {
        "id",
        "provider",
        "reportType",
        "reportDate",
        "reportTitle",
        "headHtml",
        "targetPrice",
        "targetPriceStatus",
        "investmentRating",
    }
)
_MAPPED_INDUSTRY_ROW_KEYS = frozenset(
    {"key", "name", "symbol", "marketWeight", "ytdReturn", "regMarketChangePercent"}
)
# Top-level keys of the response; each goes to either a typed column or `raw_json`.
_MAPPED_TOP_LEVEL_KEYS = frozenset(
    {
        "key",
        "name",
        "symbol",
        "sectorKey",
        "sectorName",
        "overview",
        "performance",
        "performanceOverviewBenchmark",
        "topCompanies",
        "topETFs",
        "topMutualFunds",
        "topPerformingCompanies",
        "topGrowthCompanies",
        "industries",
        "researchReports",
    }
)

MAPPED_KEYS: dict[str, frozenset[str]] = {
    "performance": _MAPPED_PERFORMANCE_KEYS,
    "performanceOverviewBenchmark": _MAPPED_BENCHMARK_KEYS,
    "topCompanies": _MAPPED_COMPANY_KEYS,
    "topETFs": _MAPPED_FUND_KEYS,
    "topMutualFunds": _MAPPED_FUND_KEYS,
    "topPerformingCompanies": _MAPPED_MOVER_KEYS,
    "topGrowthCompanies": _MAPPED_MOVER_KEYS,
    "researchReports": _MAPPED_REPORT_KEYS,
    "industries": _MAPPED_INDUSTRY_ROW_KEYS,
    "topLevel": _MAPPED_TOP_LEVEL_KEYS,
}


def warn_unmapped(
    dataset: str,
    key: str,
    block: str,
    payload: Any,
    known: frozenset[str],
) -> list[str]:
    """Logs a warning for keys outside the known set.

    No data is lost (it stays in `raw_json`); the log is a promotion
    signal for typed columns.
    """
    rows = payload if isinstance(payload, list) else [payload]
    extra: dict[str, None] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        for name in row:
            if name not in known:
                extra[str(name)] = None
    if extra:
        log.debug(
            "unmapped keys", dataset=dataset, domain_key=key, block=block, keys=sorted(extra)
        )
    return sorted(extra)


# --- fetch -----------------------------------------------------------------


def fetch_domain(key: str, domain_type: str, region: str) -> dict[str, Any]:
    """Raw sector / industry response.

    `call_yahoo`, not `call_optional`: the key came from our own discovery, so a
    404 (or a missing `payload["data"]`) is a stale taxonomy and must be `failed`.
    """
    from yfinance.data import YfData

    path = "sectors" if domain_type == "sector" else "industries"
    params = {
        "formatted": "true",
        "withReturns": "true",
        "lang": "en-US",
        "region": region,
    }
    payload = call_yahoo(
        lambda: YfData().get_raw_json(f"{_QUERY}/{path}/{key}", params=params),
        what=f"{domain_type}:{key}:{region}",
    )
    data = payload["data"]
    if not isinstance(data, dict):  # pragma: no cover - defensive
        raise KeyError("data")
    return data
