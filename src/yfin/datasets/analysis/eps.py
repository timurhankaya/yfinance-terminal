"""eps_trend + eps_revisions datasets.

The source key is `downLast7Days` (capital D) despite the docs writing it
lowercase; reading it as `d` leaves the column silently NULL.
"""

from __future__ import annotations

from yfin.core import normalize as nz
from yfin.core.families import DataFamily
from yfin.datasets.analysis.base import Column, PeriodFrameDataset
from yfin.datasets.asof_base import asof_produces
from yfin.datasets.common import to_fact_value
from yfin.datasets.exposure import ApiExposure
from yfin.datasets.registry import register


def _currency(value: object) -> str | None:
    return nz.to_str(value, max_len=8)


class EpsTrendDataset(PeriodFrameDataset):
    name = "eps_trend"
    produces = asof_produces("analyst_eps_trend")
    table = "analyst_eps_trend"
    api_method = "get_eps_trend"
    columns = (
        Column("current", "current", to_fact_value),
        # Column names cannot start with a digit: 7daysAgo -> days_ago_7
        Column("7daysAgo", "days_ago_7", to_fact_value),
        Column("30daysAgo", "days_ago_30", to_fact_value),
        Column("60daysAgo", "days_ago_60", to_fact_value),
        Column("90daysAgo", "days_ago_90", to_fact_value),
        Column("currency", "currency", _currency),
    )
    api = (
        ApiExposure(
            family=DataFamily.FUNDAMENTALS,
            table="analyst_eps_trend",
            sort_key=("as_of_date", "period"),
            descending=True,
            description="Consensus EPS estimate as it moved over time.",
        ),
    )


class EpsRevisionsDataset(PeriodFrameDataset):
    name = "eps_revisions"
    produces = asof_produces("analyst_eps_revisions")
    table = "analyst_eps_revisions"
    api_method = "get_eps_revisions"
    columns = (
        Column("upLast7days", "up_last_7d", nz.to_int),
        Column("upLast30days", "up_last_30d", nz.to_int),
        # Capital D in the source key
        Column("downLast7Days", "down_last_7d", nz.to_int),
        Column("downLast30days", "down_last_30d", nz.to_int),
        Column("currency", "currency", _currency),
    )
    api = (
        ApiExposure(
            family=DataFamily.FUNDAMENTALS,
            table="analyst_eps_revisions",
            sort_key=("as_of_date", "period"),
            descending=True,
            description="Counts of upward and downward EPS revisions.",
        ),
    )


register(EpsTrendDataset(), group="analysis")
register(EpsRevisionsDataset(), group="analysis")
