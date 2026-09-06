"""Alan tiplerinin TEK tanim yeri.

Bir `kind` uc seyi belirler: SQL kolon tipi, kaynak degerin donusumu ve
satir boyutu maliyeti. Bunlar ayri if-zincirlerine dagilirsa yeni bir kind
eklemek birden fazla dosyayi degistirmeyi gerektirir ve sessizce
ayrisabilirler. Burada tek bir tabloda toplanirlar; yeni kind eklemek
yalnizca bu tabloya satir eklemektir (OCP).
"""

from __future__ import annotations

import numbers
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.dialects.mysql import BIGINT
from sqlalchemy.types import TypeEngine

from yfin import normalize as nz
from yfin.logging_setup import get_logger
from yfin.models.base import BIG_PRECISION, BigNumType, PriceType, TsType

log = get_logger(__name__)

# utf8mb4'te VARCHAR(n) yaklasik 4n+2 byte tutar; TEXT satirdan yalnizca
# ~20 byte goturur (S5.4 satir butcesi)
_VARCHAR_COST = 4


def _string_converter(max_len: int) -> Callable[[Any], Any]:
    def convert(value: Any) -> Any:
        return nz.to_str(value, max_len=max_len)

    return convert


def _to_big(value: Any) -> Decimal | None:
    """DECIMAL(38,0): kesirli gelirse tam sayiya indirilir.

    `localcontext(prec=BIG_PRECISION)` ZORUNLUDUR: Python'un varsayilan
    context'i 28 ANLAMLI hane tasir, kolon ise 38. Varsayilanla 1e28 ve
    ustu `InvalidOperation` firlatir -- kolon 1e38'e kadar kabul ederken.
    Istisna `normalize` icinden ciktigi icin o (sembol x dataset) hucresinin
    TUM satirlarini dusururdu; prec=38 ile Python siniri kolonun gercek
    sinirina esitlenir. Gercekten tasan deger satir dusurur, hucre `ok`
    kalir (`common.key_value` deseni).
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
    """Kaynak Timestamp/datetime dondurur; epoch sayisi gelirse de kabul edilir.

    `numbers.Real` KULLANILIR, `int | float` DEGIL: `np.float64` float'in alt
    sinifidir ama `np.int64` int'in alt sinifi DEGILDIR. Duz isinstance ile
    pandas kolonu int64 oldugunda (insider_roster'da bir sembolun TUM
    tarihleri doluysa oyle olur) deger `to_datetime_utc`'ye duser, o da None
    doner: SESSIZ NULL. numpy skalarlarinin tamami numbers ABC'lerine
    kayitlidir. `bool` haric tutulur (Integral'dir ama tarih degildir).
    """
    if isinstance(value, numbers.Real) and not isinstance(value, bool):
        return nz.epoch_to_datetime(value, unit="s")
    return nz.to_datetime_utc(value)


@dataclass(frozen=True, slots=True)
class KindSpec:
    sql_type: Callable[[], TypeEngine[Any]]
    convert: Callable[[Any], Any]
    row_cost: int


KINDS: dict[str, KindSpec] = {
    "str16": KindSpec(lambda: String(16), _string_converter(16), 16 * _VARCHAR_COST + 2),
    "str32": KindSpec(lambda: String(32), _string_converter(32), 32 * _VARCHAR_COST + 2),
    "str64": KindSpec(lambda: String(64), _string_converter(64), 64 * _VARCHAR_COST + 2),
    "str128": KindSpec(lambda: String(128), _string_converter(128), 128 * _VARCHAR_COST + 2),
    "str255": KindSpec(lambda: String(255), _string_converter(255), 255 * _VARCHAR_COST + 2),
    "text": KindSpec(lambda: Text(), nz.to_str, 20),
    "dec": KindSpec(PriceType, nz.to_decimal, 13),
    "big": KindSpec(BigNumType, _to_big, 17),
    "int": KindSpec(lambda: Integer(), nz.to_int, 4),
    "ubig": KindSpec(lambda: BIGINT(unsigned=True), _to_unsigned, 8),
    "bool": KindSpec(lambda: Boolean(), nz.to_bool, 1),
    "epoch_s": KindSpec(TsType, _to_epoch_seconds, 8),
    "epoch_ms": KindSpec(TsType, _to_epoch_millis, 8),
    "dt": KindSpec(TsType, _to_datetime, 8),
}
