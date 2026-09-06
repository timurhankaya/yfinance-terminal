"""The market-wide dataset family."""

from __future__ import annotations

# The module-level `register_market(...)` runs only when the module is
# imported. If `screener` were left out here it would never register,
# and `yfin screen sync` would SILENTLY run zero datasets.
from yfin.datasets.market import calendars, screener, status  # noqa: F401
from yfin.datasets.market.base import GlobalDataset, MarketContext

__all__ = ["GlobalDataset", "MarketContext"]
