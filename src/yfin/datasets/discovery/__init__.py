"""Kesif dataset ailesi: Search + Lookup (SQ S6.4).

Modul duzeyindeki `register(...)` yalnizca modul IMPORT EDILIRSE kosar; bu
paket `yfin.datasets.__init__`in import listesindedir. Eklenmemis olsaydi
`search`/`lookup` hicbir zaman kaydolmaz ve `--datasets search` sessizce
`UnknownDatasetError` verirdi -- ki bu, bayrak kapaliyken zaten BEKLENEN
davranistir (SQ K11) ve ikisi birbirine karisirdi.
"""

from __future__ import annotations

from yfin.datasets.discovery import lookup, search  # noqa: F401
from yfin.datasets.discovery.base import (
    DISCOVERY_GATE_KEY_COLUMNS,
    DISCOVERY_GATE_TABLE,
    UNGATED_TABLES,
    DiscoveryDataset,
)

__all__ = [
    "DISCOVERY_GATE_KEY_COLUMNS",
    "DISCOVERY_GATE_TABLE",
    "UNGATED_TABLES",
    "DiscoveryDataset",
]
