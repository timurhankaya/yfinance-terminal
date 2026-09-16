"""recommendations dataset.

`recommendations_summary` is only a registry alias: the source method is a
pass-through to `get_recommendations`.
"""

from __future__ import annotations

from yfin.core import normalize as nz
from yfin.core.families import DataFamily
from yfin.datasets.analysis.base import Column, PeriodFrameDataset
from yfin.datasets.asof_base import asof_produces
from yfin.datasets.exposure import ApiExposure
from yfin.datasets.registry import register


class RecommendationsDataset(PeriodFrameDataset):
    name = "recommendations"
    produces = asof_produces("analyst_recommendations")
    table = "analyst_recommendations"
    api_method = "get_recommendations"
    # `period` is a column here, not the index (source uses a RangeIndex)
    period_column = "period"
    columns = (
        Column("strongBuy", "strong_buy", nz.to_int),
        Column("buy", "buy", nz.to_int),
        Column("hold", "hold", nz.to_int),
        Column("sell", "sell", nz.to_int),
        Column("strongSell", "strong_sell", nz.to_int),
    )
    # All five counters are NOT NULL.
    required = ("strong_buy", "buy", "hold", "sell", "strong_sell")
    api = (
        ApiExposure(
            family=DataFamily.FUNDAMENTALS,
            table="analyst_recommendations",
            sort_key=("as_of_date", "period"),
            descending=True,
            description="Buy/hold/sell recommendation counts by period.",
        ),
    )


register(RecommendationsDataset(), group="analysis")
