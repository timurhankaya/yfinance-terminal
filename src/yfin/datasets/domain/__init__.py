"""Sector / industry datasets.

Importing the submodules is what triggers the `DOMAIN_DATASETS` entries.
"""

from __future__ import annotations

from yfin.datasets.domain import profile, rankings, taxonomy  # noqa: F401
from yfin.datasets.domain.base import (
    DomainAsOfDataset,
    DomainContext,
    DomainDataset,
)
from yfin.datasets.domain.common import SECTOR_KEYS, as_of_day, fetch_domain, unwrap
from yfin.datasets.domain.payloads import DomainPayload, TaxonomyPayload

# Re-exported from `models`, where the `domains` table's own column is
# typed with it. There is one DomainType.
from yfin.models.domains import DomainType

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
