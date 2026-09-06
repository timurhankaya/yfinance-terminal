"""Alan tiplerinin TEK tanim yeri.

Bir `kind` IKI seyi belirler: SQL kolon tipi ve kaynak degerin
donusumu. (Ucuncu bir alan -- satir boyutu maliyeti -- MySQL'in 65 535
baytlik satir siniri icin vardi; PostgreSQL'de boyle bir sinir olmadigi
icin KALDIRILDI, bkz. models/columns.py.) Bunlar ayri if-zincirlerine
dagilirsa yeni bir kind
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

from sqlalchemy import BigInteger, Boolean, Integer, String, Text
from sqlalchemy.types import TypeEngine

from yfin import normalize as nz
from yfin.logging_setup import get_logger
from yfin.models.base import BIG_PRECISION, BigNumType, PriceType, TsType

log = get_logger(__name__)

def _non_negative(column: str) -> str:
    """PostgreSQL'de unsigned tamsayi YOKTUR; `BIGINT UNSIGNED`in verdigi
    "negatif olamaz" garantisi CHECK ile yeniden kurulur."""
    return f'"{column}" >= 0'


def _c_string(length: int) -> Callable[[], TypeEngine[Any]]:
    """String kolonlari da COLLATE "C" tasir.

    ATLANMASI KOLAY VE SESSIZ: MySQL'de bu kolonlara TABLO varsayilani
    (utf8mb4_0900_ai_ci) uygulaniyordu; PostgreSQL'de VERITABANI
    varsayilani (en_US.utf8) uygulanirdi -- yani ne "C" ne de eski
    davranis. models/base.py'deki fabrikalar duzeltilip burasi
    unutulsaydi ticker_info / ticker_fast_info / history_metadata'nin
    buyuk kismi yanlis collation'da kalirdi (PG S2.5).
    """
    return lambda: String(length, collation="C")


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
    # Kolon uzerindeki CHECK ifadesini ureten fabrika (kolon adi -> SQL).
    # `columns.make_column` icinde `if kind == "ubig"` seklinde SABIT bir
    # dal duruyordu; kisitli yeni bir kind eklemek IKI dosya degistirmeyi
    # gerektiriyordu (OCP ihlali). Kisit artik kind'in KENDI tanimindadir:
    # yeni kind eklemek yine yalnizca bu tabloya satir eklemektir.
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
    # PostgreSQL'de unsigned tamsayi YOKTUR. `BIGINT UNSIGNED`in verdigi
    # garanti make_column()'daki CHECK ile yeniden kurulur; buradaki
    # `_to_unsigned` (negatifi None yapar) ikinci savunma hattidir ve
    # KORUNUR -- satir dusurmek yerine hucre dusurme davranisi degismez.
    "ubig": KindSpec(lambda: BigInteger(), _to_unsigned, check=_non_negative),
    "bool": KindSpec(lambda: Boolean(), nz.to_bool),
    "epoch_s": KindSpec(TsType, _to_epoch_seconds),
    "epoch_ms": KindSpec(TsType, _to_epoch_millis),
    "dt": KindSpec(TsType, _to_datetime),
}
