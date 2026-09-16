"""earnings_estimate + revenue_estimate datasets.

Both write `analyst_estimates` with `metric` in the PK; they stay separate
datasets so each gets its own `sync_run_items` cell and `--datasets` entry.
"""

from __future__ import annotations

from yfin.core import normalize as nz
from yfin.core.families import DataFamily
from yfin.datasets.analysis.base import Column, PeriodFrameDataset
from yfin.datasets.asof_base import asof_produces
from yfin.datasets.common import to_fact_value
from yfin.datasets.exposure import ApiExposure
from yfin.datasets.registry import register
from yfin.models.analysis import EstimateMetric

TABLE = "analyst_estimates"


def _currency(value: object) -> str | None:
    return nz.to_str(value, max_len=8)


def _columns(year_ago_source: str) -> tuple[Column, ...]:
    return (
        # DECIMAL(38,10): the same column holds EPS and revenue values.
        Column("avg", "avg", to_fact_value),
        Column("low", "low", to_fact_value),
        Column("high", "high", to_fact_value),
        Column(year_ago_source, "year_ago_value", to_fact_value),
        # Source can send float (1.0) or NaN
        Column("numberOfAnalysts", "number_of_analysts", nz.to_int),
        # Can be negative
        Column("growth", "growth", nz.to_decimal),
        # Column added by yfinance; not in the official docs
        Column("currency", "currency", _currency),
    )


class _EstimateDataset(PeriodFrameDataset):
    produces = asof_produces(TABLE)
    table = TABLE
    # Unlike `financial_facts`, an all-NULL period row is still written: the
    # period set is fixed, so a NULL row means "period exists, no estimate yet".


class EarningsEstimateDataset(_EstimateDataset):
    name = "earnings_estimate"
    api_method = "get_earnings_estimate"
    constants = (("metric", EstimateMetric.EPS.value),)
    columns = _columns("yearAgoEps")
    # Both estimate datasets write analyst_estimates and only `metric`
        # separates them, so the slice has to be pinned here -- otherwise
        # asking for earnings would also return revenue rows.
    api = (
        ApiExposure(
            family=DataFamily.FUNDAMENTALS,
            table="analyst_estimates",
            sort_key=("as_of_date", "period"),
            descending=True,
            fixed=(("metric", "eps"),),
            description="Consensus earnings estimates by period.",
        ),
    )


class RevenueEstimateDataset(_EstimateDataset):
    name = "revenue_estimate"
    api_method = "get_revenue_estimate"
    constants = (("metric", EstimateMetric.REVENUE.value),)
    columns = _columns("yearAgoRevenue")
    api = (
        ApiExposure(
            family=DataFamily.FUNDAMENTALS,
            table="analyst_estimates",
            sort_key=("as_of_date", "period"),
            descending=True,
            fixed=(("metric", "revenue"),),
            description="Consensus revenue estimates by period.",
        ),
    )


register(EarningsEstimateDataset(), group="analysis")
register(RevenueEstimateDataset(), group="analysis")
