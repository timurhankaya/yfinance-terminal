"""Donem indeksli analist cerceveleri icin ortak taban (AH S6.3).

Dokuz analist dataset'inin besi ayni sekle sahiptir: goreli donem etiketi
(`0q`, `+1q`, `0y`, `+1y`, `LTG`, `0m`..`-3m`) ile anahtarlanan tek bir
cerceve. Ortak olan yalnizca SEKIL degil, iki KURAL'dir:

1. Donem etiketi GORELIDIR; `as_of_date` olmadan satir anlamsizdir. Bu
   yuzden taban `AsOfDataset`'tir.
2. Kaynak "bu sembolde bu modul yok" durumunu HTTP 404 ile bildirir; bu bir
   `empty`tir, `failed` degil -- her fetch `call_optional` kullanir (AH S8.4).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pandas as pd

from yfin import normalize as nz
from yfin.client import call_optional
from yfin.datasets.asof_base import AsOfDataset
from yfin.datasets.base import NormalizedResult, SyncContext, TableWrite
from yfin.datasets.common import key_value
from yfin.datasets.payloads import AsOfFramePayload
from yfin.logging_setup import get_logger

log = get_logger(__name__)

# models.analysis.PERIOD_LENGTH ile ayni; AsciiKeyType(8)
PERIOD_LENGTH = 8


@dataclass(frozen=True, slots=True)
class Column:
    """Kaynak anahtari -> MySQL kolonu -> tipli donusum.

    Donusturucu alanin YANINDA durur; `models/kinds.py`'nin `Field` tablosu
    burada kullanilamaz cunku bu kolonlar SQLAlchemy'de elle tanimli ve
    FactValueType gibi bir kind tablosunda karsiligi yok.
    """

    source: str
    column: str
    convert: Callable[[Any], Any]


class PeriodFrameDataset(AsOfDataset[AsOfFramePayload]):
    depends_on = ("symbols",)

    table: str
    api_method: str
    columns: tuple[Column, ...]
    # Satirin sabit bilesenleri (analyst_estimates'te metric); PK'ya girerler.
    constants: tuple[tuple[str, Any], ...] = ()
    # `period` index'te mi, bir KOLONDA mi? recommendations'ta kolondur.
    period_column: str | None = None
    # Bu kolonlardan biri NULL ise satir YAZILMAZ: NOT NULL ihlali sembol
    # basina tek transaction geregi SEMBOLUN TAMAMINI dusururdu (S8.7).
    required: tuple[str, ...] = ()

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
            # Kaynak ayni donemi iki kez verirse attempted=2 / verified=1
            # yanlis `failed` uretirdi (S8.6); son kayit kazanir.
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
