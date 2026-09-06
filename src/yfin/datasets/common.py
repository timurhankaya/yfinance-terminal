"""Helpers shared across datasets."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from yfin.core import normalize as nz
from yfin.core.logging_setup import get_logger
from yfin.models.base import FACT_PRECISION
from yfin.models.fields import Field
from yfin.models.kinds import KINDS

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


# --- shared helpers for discovery datasets --------------------------------
# The three discovery datasets (`search`, `lookup`, `screener`) each did the
# same four things, copied three times over. `domain/common.py` solves the
# same problem the same way.


def symbol_is_writable(symbol: str) -> bool:
    """Whether the symbol can be written to the `symbols` table.

    The constraint is derived from `SymbolType()` = VARCHAR(SYMBOL_LENGTH)
    COLLATE "C"; the length is not hardcoded here.

    `^` is in scope: 93 of 9,243 symbols measured start with it (indices).
    A validation that narrows the character set would reject indices wholesale.

    Not derived from write order -- computed inside `normalize` instead: an
    order-dependent derivation would silently break if the gate's scope changed.
    """
    from yfin.models.base import SYMBOL_LENGTH

    return len(symbol) <= SYMBOL_LENGTH and symbol.isascii()


def utc_as_of_day(fetched_at: datetime) -> date:
    """Derives `as_of_date` from the fetch timestamp, not from `now()`.

    Three datasets used to call `datetime.now(UTC).date()`; that made the
    gate row's `as_of_date` and `fetched_at` come from different time
    sources and diverge across midnight -- rows could get a September 5
    timestamp but land on the September 6 day. `domain/common.as_of_day`
    applies the same principle for the market timezone; discovery is
    region-independent, so it uses UTC.
    """
    moment = fetched_at if fetched_at.tzinfo is not None else fetched_at.replace(tzinfo=UTC)
    return moment.astimezone(UTC).date()


def expect_dict(value: Any, *, what: str) -> dict[str, Any]:
    """Raises loudly if the response is not a dict.

    Returning empty silently would conflate "no data" (`empty`) with "the
    response shape changed" (`failed`); the latter needs to be seen right away.
    """
    if not isinstance(value, dict):
        raise TypeError(f"{what} yaniti sozluk degil: {type(value).__name__}")
    return value


def dict_items(payload: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    """Dict items in the `payload[key]` list; other items are filtered out.

    If a source block returns an unexpected scalar, only that one row
    should drop, not the whole cell.
    """
    return [item for item in payload.get(key) or [] if isinstance(item, dict)]


def discovered_symbol_row(
    symbol: str,
    *,
    source: str,
    fetched_at: datetime,
    **typed_fields: Any,
) -> dict[str, Any]:
    """`symbols` row for a discovered symbol.

    The four common fields live here, in one place. Source-specific
    identifying fields pass through `typed_fields` -- each path supplies
    only what it actually populates, and `update_columns` is kept narrow
    to match.

    `is_active`, `discovered_by`, and `discovered_at` take effect only on
    INSERT: all three paths' `update_columns` exclude them. If they were in
    scope, a symbol an operator manually activated would silently flip back
    to inactive the next time it was rediscovered.
    """
    return {
        "symbol": symbol,
        **typed_fields,
        "is_active": False,
        "discovered_by": source,
        "discovered_at": fetched_at,
        "last_seen_at": fetched_at,
    }
