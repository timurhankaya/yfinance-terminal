"""Discovery dataset family: Search + Lookup.

This package's `__init__` exists only for its side effect: the
module-level `register(...)` calls run only if the module is imported, and
this package is in `yfin.datasets.__init__`'s import list. Without this,
neither dataset would ever register and `--datasets search` would raise
`UnknownDatasetError`.

No re-export: `datasets/domain/` and `datasets/financials/` don't either.
Consumers import directly from `yfin.datasets.discovery.base`; a second
import path would raise a free question about which one is canonical.
"""

from __future__ import annotations

from yfin.datasets.discovery import lookup, search  # noqa: F401
