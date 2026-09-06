"""Single definition point for field kinds.

A kind decides three things: the SQL column type, how a source value is
converted, and whether the column carries a CHECK. Spreading those across
separate if-chains would mean touching several files to add a kind, and
they could drift apart. Adding a kind here is adding one row to KINDS.
"""

from __future__ import annotations

import numbers
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from sqlalchemy import BigInteger, Boolean, Integer, String, Text
from sqlalchemy.types import TypeEngine

from yfin.core import normalize as nz
from yfin.core.logging_setup import get_logger
from yfin.models.base import BIG_PRECISION, BigNumType, PriceType, TsType

log = get_logger(__name__)

def _non_negative(column: str) -> str:
    """PostgreSQL has no unsigned integers; the guarantee is a CHECK."""
    return f'"{column}" >= 0'


def _c_string(length: int) -> Callable[[], TypeEngine[Any]]:
    """String columns carry COLLATE "C" too.

    Easy to miss and silent: without it these columns fall back to the
    database collation, which covers most of ticker_info,
    ticker_fast_info and history_metadata.
    """
    return lambda: String(length, collation="C")


def _string_converter(max_len: int) -> Callable[[Any], Any]:
    def convert(value: Any) -> Any:
        return nz.to_str(value, max_len=max_len)

    return convert


def _to_big(value: Any) -> Decimal | None:
    """NUMERIC(38,0): a fractional value is reduced to an integer.

    localcontext(prec=BIG_PRECISION) is required. Python's default
    context carries 28 significant digits while the column holds 38, so
    anything from 1e28 up would raise InvalidOperation on a value the
    column accepts. The exception escapes through normalize and would
    drop every row of that (symbol x dataset) cell.
    """
    dec = nz.to_decimal(value)
    if dec is None:
        return None
    try:
        with localcontext() as ctx:
            ctx.prec = BIG_PRECISION
            return dec.quantize(Decimal(1))
    except InvalidOperation:
        log.warning("big value out of range", value=str(dec)[:32])
        return None


def _to_unsigned(value: Any) -> int | None:
    parsed = nz.to_int(value)
    return None if parsed is None or parsed < 0 else parsed


def _to_epoch_seconds(value: Any) -> Any:
    return nz.epoch_to_datetime(value, unit="s")


def _to_epoch_millis(value: Any) -> Any:
    return nz.epoch_to_datetime(value, unit="ms")


def _to_datetime(value: Any) -> Any:
    """Accepts a Timestamp/datetime, or an epoch number.

    numbers.Real rather than `int | float`: np.float64 subclasses float
    but np.int64 does not subclass int. With a plain isinstance check an
    int64 pandas column (insider_roster, when every date is populated)
    would fall through to to_datetime_utc and come back None -- a silent
    NULL. bool is excluded: it is Integral but not a date.
    """
    if isinstance(value, numbers.Real) and not isinstance(value, bool):
        return nz.epoch_to_datetime(value, unit="s")
    return nz.to_datetime_utc(value)


@dataclass(frozen=True, slots=True)
class KindSpec:
    sql_type: Callable[[], TypeEngine[Any]]
    convert: Callable[[Any], Any]
    # Builds the column's CHECK expression (column name -> SQL). Keeping
    # it on the kind means make_column does not need to know which kinds
    # are constrained.
    check: Callable[[str], str] | None = None


KINDS: dict[str, KindSpec] = {
    "str16": KindSpec(_c_string(16), _string_converter(16)),
    "str32": KindSpec(_c_string(32), _string_converter(32)),
    "str64": KindSpec(_c_string(64), _string_converter(64)),
    "str128": KindSpec(_c_string(128), _string_converter(128)),
    "str255": KindSpec(_c_string(255), _string_converter(255)),
    "text": KindSpec(lambda: Text(), nz.to_str),
    "dec": KindSpec(PriceType, nz.to_decimal),
    "big": KindSpec(BigNumType, _to_big),
    "int": KindSpec(lambda: Integer(), nz.to_int),
    # _to_unsigned (negative -> None) is the second line of defence
    # behind the CHECK: it drops the cell rather than the whole row.
    "ubig": KindSpec(lambda: BigInteger(), _to_unsigned, check=_non_negative),
    "bool": KindSpec(lambda: Boolean(), nz.to_bool),
    "epoch_s": KindSpec(TsType, _to_epoch_seconds),
    "epoch_ms": KindSpec(TsType, _to_epoch_millis),
    "dt": KindSpec(TsType, _to_datetime),
}
