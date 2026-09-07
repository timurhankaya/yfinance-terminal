"""Common base for period-indexed analyst frames.

Five of the nine analyst datasets share one shape: a single frame keyed by
a relative period label (`0q`, `+1q`, `0y`, `+1y`, `LTG`, `0m`..`-3m`).
Two rules follow from that shape:

1. The period label is relative, so a row is meaningless without
   `as_of_date` -- the base class is `AsOfDataset`.
2. The source signals "module not available for this symbol" via HTTP
   404, which is an `empty` result, not `failed` -- every fetch uses
   `call_optional`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pandas as pd

from yfin.core import normalize as nz
from yfin.core.logging_setup import get_logger
from yfin.datasets.asof_base import AsOfDataset
from yfin.datasets.base import NormalizedResult, SyncContext
from yfin.datasets.common import key_value
from yfin.datasets.payloads import AsOfFramePayload
from yfin.ingest.client import call_optional
from yfin.storage.contracts import TableWrite

log = get_logger(__name__)

# Matches models.analysis.PERIOD_LENGTH; AsciiKeyType(8)
PERIOD_LENGTH = 8


@dataclass(frozen=True, slots=True)
class Column:
    """Source key -> SQL column -> typed conversion.

    The converter lives next to the field: `models/kinds.py`'s `Field` table
    does not apply here because these columns are defined by hand in
    SQLAlchemy with no counterpart in a kind table like FactValueType.
    """

    source: str
    column: str
    convert: Callable[[Any], Any]


class PeriodFrameDataset(AsOfDataset[AsOfFramePayload]):
    depends_on = ("symbols",)

    table: str
    api_method: str
    columns: tuple[Column, ...]
    # Fixed row components (e.g. `metric` in analyst_estimates); part of the PK.
    constants: tuple[tuple[str, Any], ...] = ()
    # Whether `period` lives in the index or in a column (recommendations uses a column).
    period_column: str | None = None
    # If any of these columns is NULL, the row is dropped: a NOT NULL violation
    # would drop the whole symbol, since each symbol writes in one transaction.
    required: tuple[str, ...] = ()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Names the one table this family writes as its gate source.

        Set BEFORE delegating, because the delegate is `AsOfGate`'s check
        for exactly this declaration. Derived from `table` rather than
        spelled out again in seven subclasses: the two would drift, and a
        `gate_source_tables` naming a table the dataset does not write
        fails every run of it.
        """
        if "table" in cls.__dict__:
            cls.gate_source_tables = (cls.__dict__["table"],)
        super().__init_subclass__(**kwargs)

    @property
    def key_columns(self) -> tuple[str, ...]:
        return ("symbol", "as_of_date", *(name for name, _ in self.constants), "period")

    def fetch(self, ctx: SyncContext) -> AsOfFramePayload:
        frame = call_optional(
            getattr(ctx.ticker, self.api_method), what=f"{self.name}:{ctx.symbol}"
        )
        return AsOfFramePayload(frame=frame, fetched_at=ctx.fetched_at)

    def normalize(self, raw: AsOfFramePayload, symbol: str) -> NormalizedResult:
        frame = raw.frame
        if nz.is_empty_result(frame):
            return NormalizedResult()
        assert isinstance(frame, pd.DataFrame)

        as_of = raw.fetched_at.date()
        rows: dict[str, dict[str, Any]] = {}

        for index, record in frame.iterrows():
            source = record.get(self.period_column) if self.period_column else index
            period = key_value(
                source, PERIOD_LENGTH, field="period", dataset=self.name, symbol=symbol
            )
            if period is None:
                continue

            row: dict[str, Any] = {
                "symbol": symbol,
                "as_of_date": as_of,
                **dict(self.constants),
                "period": period,
            }
            for column in self.columns:
                row[column.column] = column.convert(record.get(column.source))

            missing = [name for name in self.required if row.get(name) is None]
            if missing:
                log.warning(
                    "required column missing; row dropped",
                    dataset=self.name,
                    symbol=symbol,
                    period=period,
                    columns=missing,
                )
                continue

            row["fetched_at"] = raw.fetched_at
            # If the source returns the same period twice, attempted=2 /
            # verified=1 would falsely read as `failed`; last record wins.
            rows[period] = row

        if not rows:
            return NormalizedResult()

        return NormalizedResult(
            writes=[
                TableWrite(
                    table=self.table,
                    rows=list(rows.values()),
                    key_columns=self.key_columns,
                    update_columns=(*(c.column for c in self.columns), "fetched_at"),
                )
            ]
        )
