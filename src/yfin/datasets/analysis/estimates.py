"""earnings_estimate + revenue_estimate dataset'leri (AH S6.3).

Ikisi TEK tabloya (`analyst_estimates`) yazar ve `metric` ENUM'u PK'nin
bilesenidir: kolon setleri birebir ayni ve ikisi de AYNI Yahoo modulunden
(`earningsTrend`) geliyor. Ayri kayit kalirlar cunku ayri `sync_run_items`
hucresi ve ayri `--datasets` secilebilirligi gerekir.

Tek fark "gecen yilki deger"in kaynak anahtaridir: `yearAgoEps` /
`yearAgoRevenue` (base.py:325-334).
"""

from __future__ import annotations

from yfin import normalize as nz
from yfin.datasets.analysis.base import Column, PeriodFrameDataset
from yfin.datasets.asof_base import asof_produces
from yfin.datasets.common import to_fact_value
from yfin.datasets.registry import register
from yfin.models.analysis import EstimateMetric

TABLE = "analyst_estimates"


def _currency(value: object) -> str | None:
    return nz.to_str(value, max_len=8)


def _columns(year_ago_source: str) -> tuple[Column, ...]:
    return (
        # DECIMAL(38,10) zorunlu: AYNI kolonda AAPL EPS 1.97656 ve THYAO
        # revenue 1_285_436_390_920 bulunur.
        Column("avg", "avg", to_fact_value),
        Column("low", "low", to_fact_value),
        Column("high", "high", to_fact_value),
        Column(year_ago_source, "year_ago_value", to_fact_value),
        # Kaynakta float (1.0) ve NaN gelebiliyor
        Column("numberOfAnalysts", "number_of_analysts", nz.to_int),
        # Negatif olabilir
        Column("growth", "growth", nz.to_decimal),
        # yfinance'in ekledigi kolon; resmi dokumantasyonda yok
        Column("currency", "currency", _currency),
    )


class _EstimateDataset(PeriodFrameDataset):
    produces = asof_produces(TABLE)
    table = TABLE
    # `financial_facts`ten BILINCLI fark: tamami NULL olan donem satiri yine
    # de yazilir. Orada satirin yoklugu "kalem yok" demekti; burada donem
    # seti sabit dortludur ve NULL satir "donem var, tahmin yok" bilgisini
    # tasir. THYAO'nun 0q/+1q donemleri tam olarak boyle olculdu.


class EarningsEstimateDataset(_EstimateDataset):
    name = "earnings_estimate"
    api_method = "get_earnings_estimate"
    constants = (("metric", EstimateMetric.EPS.value),)
    columns = _columns("yearAgoEps")


class RevenueEstimateDataset(_EstimateDataset):
    name = "revenue_estimate"
    api_method = "get_revenue_estimate"
    constants = (("metric", EstimateMetric.REVENUE.value),)
    columns = _columns("yearAgoRevenue")


register(EarningsEstimateDataset())
register(RevenueEstimateDataset())
