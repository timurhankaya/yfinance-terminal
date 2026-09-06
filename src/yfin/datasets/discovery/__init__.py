"""Kesif dataset ailesi: Search + Lookup (SQ S6.4).

Bu paketin `__init__`i YALNIZCA YAN ETKI icindir: modul duzeyindeki
`register(...)` cagrilari ancak modul import edilirse kosar ve bu paket
`yfin.datasets.__init__`in import listesindedir. Eklenmemis olsaydi iki
dataset hicbir zaman kaydolmaz, `--datasets search` `UnknownDatasetError`
verirdi.

RE-EXPORT YOKTUR: `datasets/domain/` ve `datasets/financials/` de tutmuyor.
Tuketiciler `yfin.datasets.discovery.base`ten dogrudan alir; ikinci bir
import yolu, hangisinin kanonik oldugu sorusunu bedava yaratirdi.
"""

from __future__ import annotations

from yfin.datasets.discovery import lookup, search  # noqa: F401
