"""Fetch outputs of domain datasets.

`fetched_at` and `as_of_date` travel in the payload so `normalize` never
calls `datetime.now()` itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True)
class DomainPayload:
    """Raw response for a single (key, region) pair."""

    data: dict[str, Any]
    fetched_at: datetime
    as_of_date: date
    region: str
    # Region-less datasets do not write `region` to their rows; this field
    # only feeds the PK of region-scoped tables.
    domain_type: str = "sector"
    # Parent sector key in the DB (used only for industry profile)
    expected_parent: str | None = None


@dataclass(frozen=True)
class TaxonomyPayload:
    """Raw response for all 11 sectors; bootstrap processes them in one pass."""

    sectors: dict[str, dict[str, Any]] = field(default_factory=dict)
    fetched_at: datetime = datetime.min
