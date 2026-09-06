"""API tables. Importing this module registers them on Base.metadata.

They share the pipeline's `Base` on purpose: one database, one metadata,
one Alembic history. `migrations/env.py` imports this package for exactly
that reason -- without the import, autogenerate would see the API tables
as absent and cheerfully write a migration that drops them.
"""

from __future__ import annotations

from yfin.api.models.clients import (
    CLIENT_ID_LENGTH,
    CLIENT_ID_PREFIX,
    ApiClient,
    ApiClientScope,
    ApiClientSecret,
    ApiScope,
)
from yfin.api.models.plans import ApiPlan, ApiUsageDaily, UsageFamily

__all__ = [
    "CLIENT_ID_LENGTH",
    "CLIENT_ID_PREFIX",
    "ApiClient",
    "ApiClientScope",
    "ApiClientSecret",
    "ApiPlan",
    "ApiScope",
    "ApiUsageDaily",
    "UsageFamily",
]
