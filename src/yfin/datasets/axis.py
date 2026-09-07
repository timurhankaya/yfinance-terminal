"""Which of the three registries a dataset was registered in.

Not a property of the dataset: it is exactly the registry it lives in,
and the three registries are the three loops the pipeline runs (a symbol
loop, a region/variant loop, a domain-key loop).

One enum rather than two vocabularies. The pipeline spelled these as
`Turn.kind` ("market" / "domain") and the API spelled the same three
values as `catalog.SCOPE_*`, one of which is published on the wire. A
fourth axis would have had to be added in both, and nothing would have
failed if it were added in only one.
"""

from __future__ import annotations

from enum import StrEnum


class DatasetAxis(StrEnum):
    SYMBOL = "symbol"
    MARKET = "market"
    DOMAIN = "domain"
