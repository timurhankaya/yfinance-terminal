"""Piyasa-kapsamli dataset ailesi (S6.4, S6.5)."""

from __future__ import annotations

# SQ S6.4: modul duzeyindeki `register_market(...)` yalnizca modul
# IMPORT EDILIRSE kosar. `screener` buraya eklenmezse hicbir zaman
# kaydolmaz ve `yfin screen sync` SESSIZCE sifir dataset kosardi.
from yfin.datasets.market import calendars, screener, status  # noqa: F401
from yfin.datasets.market.base import GlobalDataset, MarketContext

__all__ = ["GlobalDataset", "MarketContext"]
