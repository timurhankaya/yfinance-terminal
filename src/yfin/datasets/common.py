"""Helpers shared across datasets."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

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
        log.warning("unmapped keys", dataset=dataset, keys=unmapped)
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

# NUMERIC(38,10): PostgreSQL silently rounds the 11th digit (measured), so
# rounding is done deliberately on the Python side instead.
FACT_QUANTUM = Decimal("1E-10")


def to_fact_value(value: Any) -> Decimal | None:
    """Quantizes a value for FactValueType() = DECIMAL(38,10).

    `localcontext(prec=FACT_PRECISION)` is required: Python's default
    context carries 28 significant digits while the column holds 38 (28
    integer + 10 fractional). With the default, 1e18 raises
    `InvalidOperation` even though the column accepts up to 1e28. That
    exception would escape `normalize` into `runner` and drop every row of
    that (symbol x dataset) cell -- one oversized value costing the whole
    period's line items. With prec=38, Python's limit matches the column's
    actual limit; only a value that truly overflows drops its row, and the
    cell of the dataset stays `ok` (the `key_value` pattern).
    """
    dec = nz.to_decimal(value)
    if dec is None:
        return None
    try:
        with localcontext() as ctx:
            ctx.prec = FACT_PRECISION
            return dec.quantize(FACT_QUANTUM)
    except InvalidOperation:
        log.warning("fact value out of range", value=str(dec)[:32])
        return None


def blank_to_none(value: Any, *, max_len: int | None = None) -> str | None:
    """Sentinel blank string -> NULL.

    The source signals "no value" as `''` for `ToGrade`, `Position`,
    `Transaction`, `URL`, and `priceTargetAction`. A plain `to_str` would
    write that as an empty string, losing the distinction between "blank"
    and "unknown".
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

    Never truncated: a truncated key would collapse two distinct records
    into one row, a silent data loss. Dropping the row is the pattern
    instead -- the cell is not marked `failed` just because one key is bad,
    so the same call's valid rows are not lost with it; the loss is logged.
    """
    text = nz.to_str(value, max_len=None)
    if text is not None:
        text = text.strip()
    if not text:
        log.warning("empty key field", dataset=dataset, symbol=symbol, field=field)
        return None
    if len(text) > max_len:
        log.warning(
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

    PostgreSQL silently rounds the fractional part (measured); rounding
    here instead keeps `kinds.py`'s `big` rule as the single source of truth.
    """
    return KINDS["big"].convert(value)


def to_datetime_value(value: Any) -> Any:
    """TsType() column; the source sends either `datetime64` or raw epoch `float64`.

    `kinds.py`'s `dt` rule accepts both forms; `insider_roster`'s Position
    Direct/Indirect Date fields switch between them depending on the
    symbol (measured populated float on 6 symbols).
    """
    return KINDS["dt"].convert(value)


def date_range_kwargs(start: date | None, end: date | None) -> dict[str, str]:
    """yfinance call arguments for `date_range="api"` datasets.

    The lower bound is set explicitly: calling with `start=None` makes
    yfinance apply its own default window (`end - 548 days` for
    `get_shares_full`), which would silently narrow a run that only gave
    `--end`.

    The upper bound is shifted one day forward: Yahoo reads `period2` as
    exclusive, but `--end 2018-12-31` must include that day.
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

    Five datasets need this, because five tables carry a symbol with NO
    foreign key: news_symbols, fund_top_holdings, the two domain ranking
    tables, and the market status/summary boards. The FK is absent on
    purpose -- each symbol runs in a single transaction, so one foreign
    symbol would roll back everything else that symbol wrote.

    It was written five times in two different shapes: three returned new
    TableWrites, two mutated `result.writes` in place. Same policy, two
    semantics, and the copying ones each rebuilt TableWrite field by field
    and dropped `monotonic_columns` in the process. This is the copying
    shape for all five; nothing mutates its input.

    One `known_symbols` call covers every write, not one per write.
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
