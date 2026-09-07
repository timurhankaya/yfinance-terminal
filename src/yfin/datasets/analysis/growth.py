"""The growth_estimates dataset.

The official documentation (base.py:369-371) gives the index as
`0q +1q 0y +1y +5y -5y` and the columns as `stock industry sector index`.
Measurement refuted both: the index is `0q,+1q,0y,+1y,LTG`, and the only
columns returned are `stockTrend` and `indexTrend` (19 of 19 symbols).
The `industryTrend`/`sectorTrend` columns are created anyway -- the module
is requested explicitly (analysis.py:141), so if the endpoint starts
returning them again no migration is needed.
"""

from __future__ import annotations

from yfin.core import normalize as nz
from yfin.core.families import DataFamily
from yfin.datasets.analysis.base import Column, PeriodFrameDataset
from yfin.datasets.asof_base import asof_produces
from yfin.datasets.exposure import ApiExposure
from yfin.datasets.registry import register


class GrowthEstimatesDataset(PeriodFrameDataset):
    name = "growth_estimates"
    produces = asof_produces("analyst_growth_estimates")
    table = "analyst_growth_estimates"
    api_method = "get_growth_estimates"
    columns = (
        Column("stockTrend", "stock_trend", nz.to_decimal),
        # Every symbol returns the SAME value (a market index trend);
        # storing it denormalized per symbol is deliberate.
        Column("indexTrend", "index_trend", nz.to_decimal),
        Column("industryTrend", "industry_trend", nz.to_decimal),
        Column("sectorTrend", "sector_trend", nz.to_decimal),
    )
    api = (
        ApiExposure(
            family=DataFamily.FUNDAMENTALS,
            table="analyst_growth_estimates",
            sort_key=("as_of_date", "period"),
            descending=True,
            description="Consensus growth estimates by period.",
        ),
    )


register(GrowthEstimatesDataset())
