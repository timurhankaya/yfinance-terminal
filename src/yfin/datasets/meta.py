"""The minimal dataset interface the runners' audit-recording code shares."""

from __future__ import annotations

from typing import Protocol


class DatasetMeta(Protocol):
    name: str
    produces: tuple[str, ...]
