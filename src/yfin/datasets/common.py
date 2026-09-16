"""Helpers shared across datasets."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from yfin.core import metrics
from yfin.core import normalize as nz
from yfin.core.logging_setup import get_logger
from yfin.models.base import FACT_PRECISION
from yfin.models.fields import Field
from yfin.models.kinds import KINDS
from yfin.storage.contracts import SymbolLookup, TableWrite

log = get_logger(__name__)

# Explicit lower bound instead of yfinance's own default window
EPOCH_START = date(1970, 1, 1)


def convert_field(field: Field, value: Any) -> Any:
    """Typed conversion based on Field.kind.

    The converter is defined alongside the column type (models/kinds.py),
    which removes any chance of the two drifting apart.
    """
    return KINDS[field.kind].convert(value)


def project_fields(payload: Mapping[str, Any], fields: tuple[Field, ...]) -> dict[str, Any]:
    """Reduces the source dict to a dict of typed columns."""
    row: dict[str, Any] = {}
    for field in fields:
        row[field.column] = convert_field(field, payload.get(field.source))
    return row


def warn_unmapped(
    payload: Mapping[str, Any],
    fields: tuple[Field, ...],
    *,
    dataset: str,
    ignore: frozenset[str] = frozenset(),
) -> list[str]:
    """Logs unmapped keys. No data loss occurs since the raw payload is
    kept in raw_json; the log is only a promotion signal."""
    mapped = {f.source for f in fields}
    unmapped = sorted(k for k in payload if k not in mapped and k not in ignore)
    if unmapped:
        metrics.inc("yfin_sync_normalize_notes_total", kind="unmapped_keys")
        log.debug("unmapped keys", dataset=dataset, keys=unmapped)
    return unmapped


def snapshot_rows(
    symbol: str,
    payload: Mapping[str, Any],
    fields: tuple[Field, ...],
    fetched_at: Any,
) -> tuple[dict[str, Any], str]:
    """(row, content_hash). The canonical JSON feeds both the hash and
    raw_json, so the hash can be re-verified from the body read back from
    the DB."""
    canonical = nz.canonical_json(payload)
    digest = nz.content_hash(canonical=canonical)
    row = {"symbol": symbol, **project_fields(payload, fields)}
    row["raw_json"] = canonical
    row["content_hash"] = digest
    row["fetched_at"] = fetched_at
    return row, digest


def data_columns(fields: tuple[Field, ...], *, extra: tuple[str, ...] = ()) -> tuple[str, ...]:
    return tuple(f.column for f in fields) + extra


# --- shared AH normalization rules --------------------------------

# NUMERIC(38,10): PostgreSQL silently rounds the 11th digit, so rounding
# is done on the Python side instead.
FACT_QUANTUM = Decimal("1E-10")


def to_fact_value(value: Any) -> Decimal | None:
    """Quantizes a value for FactValueType() = DECIMAL(38,10).

    `localcontext(prec=FACT_PRECISION)` matches the column's 38 digits; the
    default 28 raises `InvalidOperation` on accepted values and drops the cell.
    """
    dec = nz.to_decimal(value)
    if dec is None:
        return None
    try:
        with localcontext() as ctx:
            ctx.prec = FACT_PRECISION
            return dec.quantize(FACT_QUANTUM)
    except InvalidOperation:
        metrics.inc("yfin_sync_normalize_notes_total", kind="out_of_range")
        log.debug("fact value out of range", value=str(dec)[:32])
        return None


def blank_to_none(value: Any, *, max_len: int | None = None) -> str | None:
    """Sentinel blank string -> NULL.

    The source signals "no value" as `''` for several text fields.
    """
    text = nz.to_str(value, max_len=max_len)
    if text is None:
        return None
    stripped = text.strip()
    return stripped or None


def key_value(
    value: Any,
    max_len: int,
    *,
    field: str,
    dataset: str,
    symbol: str,
) -> str | None:
    """Text going into a PK component; returns None + warns if over the limit.

    Never truncated: a truncated key would collapse distinct records into
    one row. The row is dropped and logged; the cell is not failed.
    """
    text = nz.to_str(value, max_len=None)
    if text is not None:
        text = text.strip()
    if not text:
        metrics.inc("yfin_sync_normalize_notes_total", kind="empty_key")
        log.debug("empty key field", dataset=dataset, symbol=symbol, field=field)
        return None
    if len(text) > max_len:
        metrics.inc("yfin_sync_normalize_notes_total", kind="key_too_long")
        log.debug(
            "key field too long",
            dataset=dataset,
            symbol=symbol,
            field=field,
            value=text[:64],
            length=len(text),
        )
        return None
    return text


def in_range(value: date | None, start: date | None, end: date | None) -> bool:
    """Filter used by `date_range="filter"`.

    No row is filtered if no range is given (None/None): an unfiltered run
    writes all history Yahoo returns.
    """
    if value is None:
        return False
    if start is not None and value < start:
        return False
    return not (end is not None and value > end)


def to_big_value(value: Any) -> Any:
    """BigNumType() = DECIMAL(38,0); the fractional part is rounded in Python.

    PostgreSQL silently rounds the fractional part; rounding here keeps
    `kinds.py`'s `big` rule as the single source of truth.
    """
    return KINDS["big"].convert(value)


def to_datetime_value(value: Any) -> Any:
    """TsType() column; the source sends either `datetime64` or raw epoch `float64`."""
    return KINDS["dt"].convert(value)


def date_range_kwargs(start: date | None, end: date | None) -> dict[str, str]:
    """yfinance call arguments for `date_range="api"` datasets.

    `start` is always set, else yfinance applies its own default window.
    `end` is shifted a day forward because Yahoo reads `period2` as exclusive.
    """
    kwargs = {"start": (start or EPOCH_START).isoformat()}
    if end is not None:
        kwargs["end"] = (end + timedelta(days=1)).isoformat()
    return kwargs


def mark_known(
    writer: SymbolLookup,
    writes: Sequence[TableWrite],
    *,
    column: str = "symbol",
) -> list[TableWrite]:
    """Fills `is_known` for symbols that may sit outside the universe.

    For tables that carry a symbol with no FK (a foreign symbol would roll
    back the whole transaction). Returns new writes; input is not mutated.
    """
    candidates = {row[column] for write in writes for row in write.rows if row.get(column)}
    known = writer.known_symbols(candidates) if candidates else set()
    return [
        replace(
            write,
            rows=[
                {**row, "is_known": bool(row.get(column)) and row[column] in known}
                for row in write.rows
            ],
        )
        for write in writes
    ]
