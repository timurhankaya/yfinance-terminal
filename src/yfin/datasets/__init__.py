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
    options,
    shares_full,
    symbols,
)
from yfin.datasets.registry import (
    DOMAIN_DATASETS,
    MARKET_DATASETS,
    SYMBOL_DATASETS,
    DependencyCycleError,
    UnknownDatasetError,
)

__all__ = [
    "DOMAIN_DATASETS",
    "DependencyCycleError",
    "MARKET_DATASETS",
    "SYMBOL_DATASETS",
    "UnknownDatasetError",
]
