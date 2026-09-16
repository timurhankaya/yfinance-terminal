"""Which of the three registries a dataset was registered in.

The three registries are the pipeline's three loops (symbol, region/variant,
domain key); the API publishes the same values, so one enum serves both.
"""

from __future__ import annotations

from enum import StrEnum


class DatasetAxis(StrEnum):
    SYMBOL = "symbol"
    MARKET = "market"
    DOMAIN = "domain"
