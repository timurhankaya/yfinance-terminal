"""Importing the submodules is what triggers their registry entries."""

from __future__ import annotations

from yfin.datasets import (  # noqa: F401
    analysis,
    bars,
    corporate_actions,
    discovery,
    domain,
    fast_info,
    financials,
    funds,
    history,
    history_metadata,
    holders,
    info,
    isin,
    market,
    news,
    shares_full,
    symbols,
)
from yfin.datasets.asof_base import AsOfDataset, AsOfGate
from yfin.datasets.base import Dataset, NormalizedResult, SyncContext
from yfin.datasets.domain.base import (
    DomainAsOfDataset,
    DomainContext,
    DomainDataset,
)
from yfin.datasets.registry import (
    DOMAIN_DATASETS,
    MARKET_DATASETS,
    SYMBOL_DATASETS,
    DependencyCycleError,
    Registry,
    UnknownDatasetError,
    register,
    register_domain,
    register_market,
)
from yfin.storage.contracts import TableWrite, WriteStats

__all__ = [
    "AsOfDataset",
    "AsOfGate",
    "DOMAIN_DATASETS",
    "Dataset",
    "DependencyCycleError",
    "DomainAsOfDataset",
    "DomainContext",
    "DomainDataset",
    "MARKET_DATASETS",
    "NormalizedResult",
    "Registry",
    "SYMBOL_DATASETS",
    "SyncContext",
    "TableWrite",
    "UnknownDatasetError",
    "WriteStats",
    "register",
    "register_domain",
    "register_market",
]
