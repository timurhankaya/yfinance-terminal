"""earnings_estimate + revenue_estimate datasets.

Both write to one table (`analyst_estimates`) with `metric` as part of the
PK: the column sets are identical and both come from the same Yahoo module
(`earningsTrend`). They stay separate datasets because each needs its own
`sync_run_items` cell and independent `--datasets` selection.

The only difference is the source key for "value a year ago": `yearAgoEps`
vs `yearAgoRevenue`.
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
        # DECIMAL(38,10) is required: the same column holds AAPL EPS 1.97656
        # and THYAO revenue 1_285_436_390_920.
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
    # Deliberate difference from `financial_facts`: an all-NULL period row is
    # still written. There, a missing row meant "no such line item"; here the
    # period set is a fixed four, so a NULL row means "period exists, no
    # estimate yet" -- measured exactly this way for THYAO's 0q/+1q periods.


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
