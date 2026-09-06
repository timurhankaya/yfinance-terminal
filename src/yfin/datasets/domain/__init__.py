"""Sektor / endustri dataset'leri (SI S7).

Alt modulleri import etmek `DOMAIN_DATASETS` kayitlarini tetikler.
"""

from __future__ import annotations

from yfin.datasets.domain import profile, rankings, taxonomy  # noqa: F401
from yfin.datasets.domain.base import (
    DomainAsOfDataset,
    DomainContext,
    DomainDataset,
    DomainType,
)
from yfin.datasets.domain.common import SECTOR_KEYS, as_of_day, fetch_domain, unwrap
from yfin.datasets.domain.payloads import DomainPayload, TaxonomyPayload

__all__ = [
    "SECTOR_KEYS",
    "DomainAsOfDataset",
    "DomainContext",
    "DomainDataset",
    "DomainPayload",
    "DomainType",
    "TaxonomyPayload",
    "as_of_day",
    "fetch_domain",
    "unwrap",
]
