"""recommendations dataset'i (AH S6.3).

`recommendations_summary` AYRI BIR DATASET DEGILDIR: kaynakta
`get_recommendations_summary` govdesi `return self.get_recommendations(...)`
(base.py:220-221). Registry'de ALIAS olarak durur.

Satir sayisi DEGISKENDIR: 19 sembolun 10'unda 4, 9'unda 3 donem geldi.
"""

from __future__ import annotations

from yfin import normalize as nz
from yfin.datasets.analysis.base import Column, PeriodFrameDataset
from yfin.datasets.asof_base import asof_produces
from yfin.datasets.registry import register


class RecommendationsDataset(PeriodFrameDataset):
    name = "recommendations"
    produces = asof_produces("analyst_recommendations")
    table = "analyst_recommendations"
    api_method = "get_recommendations"
    # `period` burada INDEX degil KOLONDUR (kaynak RangeIndex kullaniyor)
    period_column = "period"
    columns = (
        Column("strongBuy", "strong_buy", nz.to_int),
        Column("buy", "buy", nz.to_int),
        Column("hold", "hold", nz.to_int),
        Column("sell", "sell", nz.to_int),
        Column("strongSell", "strong_sell", nz.to_int),
    )
    # Bes sayac da NOT NULL; 19/19 sembolde int64 ve NaN yok olculdu.
    required = ("strong_buy", "buy", "hold", "sell", "strong_sell")


register(RecommendationsDataset())
