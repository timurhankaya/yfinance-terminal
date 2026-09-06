"""The minimal dataset interface the two runners share.

`_record_items` and `_failed_records` read only `name` and `produces`;
this protocol is what lets the symbol (`Dataset`) and market
(`GlobalDataset`) datasets share the same audit-recording code.
"""

from __future__ import annotations

from typing import Protocol


class DatasetMeta(Protocol):
    name: str
    produces: tuple[str, ...]
