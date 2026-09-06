"""Iki runner'in paylastigi asgari dataset arayuzu (S6.4).

`_record_items` ve `_failed_records` yalnizca `name` ve `produces` okur;
bu protokol sayesinde sembol (`Dataset`) ve piyasa (`GlobalDataset`)
dataset'leri ayni denetim kaydi kodunu paylasir.
"""

from __future__ import annotations

from typing import Protocol


class DatasetMeta(Protocol):
    name: str
    produces: tuple[str, ...]
