"""eps_trend + eps_revisions dataset'leri (AH S6.3).

`downLast7Days` ANAHTARINDA D BUYUKTUR. Kaynak 19/19 sembolde boyle
donuyor; resmi dokumantasyon (base.py:355) dordunu de kucuk yaziyor. Kucuk
`d` ile okunursa kolon SESSIZCE hep NULL kalir -- bu dosyanin en kirilgan
tek satiri budur ve fixture testiyle baglanmistir.
"""

from __future__ import annotations

from yfin import normalize as nz
from yfin.datasets.analysis.base import Column, PeriodFrameDataset
from yfin.datasets.asof_base import asof_produces
from yfin.datasets.common import to_fact_value
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
        # Kolon adi rakamla baslayamaz: 7daysAgo -> days_ago_7
        Column("7daysAgo", "days_ago_7", to_fact_value),
        Column("30daysAgo", "days_ago_30", to_fact_value),
        Column("60daysAgo", "days_ago_60", to_fact_value),
        Column("90daysAgo", "days_ago_90", to_fact_value),
        Column("currency", "currency", _currency),
    )


class EpsRevisionsDataset(PeriodFrameDataset):
    name = "eps_revisions"
    produces = asof_produces("analyst_eps_revisions")
    table = "analyst_eps_revisions"
    api_method = "get_eps_revisions"
    columns = (
        Column("upLast7days", "up_last_7d", nz.to_int),
        Column("upLast30days", "up_last_30d", nz.to_int),
        # D BUYUK -- 19/19 sembolde olculdu
        Column("downLast7Days", "down_last_7d", nz.to_int),
        Column("downLast30days", "down_last_30d", nz.to_int),
        Column("currency", "currency", _currency),
    )


register(EpsTrendDataset())
register(EpsRevisionsDataset())
