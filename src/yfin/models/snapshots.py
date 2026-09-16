"""Snapshot tables: ticker_info(_history), ticker_fast_info(_history),
history_metadata. Defined as Core Tables: the column set is generated
from fields.py, and upsert already uses a Core insert."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Column, ForeignKey, Index, Table, desc, text

from yfin.models.base import (
    Base,
    HashType,
    RawJsonType,
    SymbolType,
    TsType,
)
from yfin.models.columns import make_column
from yfin.models.fields import FAST_INFO_FIELDS, HISTORY_METADATA_FIELDS, INFO_FIELDS, Field


def _symbol_fk(primary_key: bool) -> Column[Any]:
    return Column(
        "symbol",
        SymbolType(),
        ForeignKey("symbols.symbol", onupdate="CASCADE", ondelete="RESTRICT"),
        primary_key=primary_key,
        nullable=False,
    )


def _snapshot_table(
    name: str, fields: tuple[Field, ...], *, historical: bool
) -> Table:
    cols: list[Column[Any]] = [_symbol_fk(primary_key=True)]
    if historical:
        # PK is (symbol, fetched_at); the microsecond precision means two
        # snapshots in the same second do not collide.
        cols.append(Column("fetched_at", TsType(), primary_key=True, nullable=False))
    cols.extend(make_column(f, name) for f in fields)
    cols.append(Column("raw_json", RawJsonType(), nullable=False))
    cols.append(Column("content_hash", HashType(), nullable=False))
    if not historical:
        cols.append(Column("fetched_at", TsType(), nullable=False))
    args: list[object] = list(cols)
    if historical:
        # Finds the latest snapshot via (symbol, fetched_at DESC).
        args.append(Index(f"ix_{name}_symbol_fetched", "symbol", desc(text("fetched_at"))))
    return Table(name, Base.metadata, *args)  # type: ignore[arg-type]


ticker_info = _snapshot_table("ticker_info", INFO_FIELDS, historical=False)
ticker_info_history = _snapshot_table("ticker_info_history", INFO_FIELDS, historical=True)

ticker_fast_info = _snapshot_table("ticker_fast_info", FAST_INFO_FIELDS, historical=False)
ticker_fast_info_history = _snapshot_table(
    "ticker_fast_info_history", FAST_INFO_FIELDS, historical=True
)

# history_metadata keeps no history; PK is symbol.
history_metadata = _snapshot_table("history_metadata", HISTORY_METADATA_FIELDS, historical=False)
