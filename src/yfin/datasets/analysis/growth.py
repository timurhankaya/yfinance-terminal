"""growth_estimates dataset'i (AH S6.3).

Resmi dokumantasyon (base.py:369-371) index'i `0q +1q 0y +1y +5y -5y`,
kolonlari `stock industry sector index` diye yaziyor. OLCUM ikisini de
curuttu: index `0q,+1q,0y,+1y,LTG`; kolonlar YALNIZCA `stockTrend` ve
`indexTrend` (19/19 sembol). `industryTrend`/`sectorTrend` kolonlari yine de
acilir -- modul acikca isteniyor (analysis.py:141), uc geri acildiginda
migration gerekmesin diye.
"""

from __future__ import annotations

from yfin.core import normalize as nz
from yfin.datasets.analysis.base import Column, PeriodFrameDataset
from yfin.datasets.asof_base import asof_produces
from yfin.datasets.registry import register


class GrowthEstimatesDataset(PeriodFrameDataset):
    name = "growth_estimates"
    produces = asof_produces("analyst_growth_estimates")
    table = "analyst_growth_estimates"
    api_method = "get_growth_estimates"
    columns = (
        Column("stockTrend", "stock_trend", nz.to_decimal),
        # TUM sembollerde AYNI deger gelir (piyasa endeksi trendi); sembol
        # basina denormalize saklanmasi bilinclidir (AH S5.1).
        Column("indexTrend", "index_trend", nz.to_decimal),
        Column("industryTrend", "industry_trend", nz.to_decimal),
        Column("sectorTrend", "sector_trend", nz.to_decimal),
    )


register(GrowthEstimatesDataset())
