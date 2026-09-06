"""Dataset'ler arasi ortak yardimcilar."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from yfin import normalize as nz
from yfin.logging_setup import get_logger
from yfin.models.base import FACT_PRECISION
from yfin.models.fields import Field
from yfin.models.kinds import KINDS

log = get_logger(__name__)

# yfinance'in kendi varsayilan penceresi yerine acik alt sinir (S6.3 #7)
EPOCH_START = date(1970, 1, 1)


def convert_field(field: Field, value: Any) -> Any:
    """Field.kind'a gore tipli donusum (S8.3, S8.4).

    Donusturucu, kolon tipiyle ayni yerde tanimlidir (models/kinds.py);
    ikisinin ayrisma ihtimali boylece ortadan kalkar.
    """
    return KINDS[field.kind].convert(value)


def project_fields(payload: Mapping[str, Any], fields: tuple[Field, ...]) -> dict[str, Any]:
    """Kaynak sozlugu tipli kolon sozlugune indirger."""
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
    """S8.5: haritalanmamis anahtarlari loglar. Ham veri raw_json'da oldugu
    icin VERI KAYBI YOKTUR; log yalnizca terfi sinyalidir."""
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
    """(satir, content_hash). Kanonik JSON hem hash'e hem raw_json'a gider,
    boylece DB'den okunan govdeden hash yeniden dogrulanabilir (S7.2)."""
    canonical = nz.canonical_json(payload)
    digest = nz.content_hash(canonical=canonical)
    row = {"symbol": symbol, **project_fields(payload, fields)}
    row["raw_json"] = canonical
    row["content_hash"] = digest
    row["fetched_at"] = fetched_at
    return row, digest


def data_columns(fields: tuple[Field, ...], *, extra: tuple[str, ...] = ()) -> tuple[str, ...]:
    return tuple(f.column for f in fields) + extra


# --- AH S8.3 ortak normalizasyon kurallari --------------------------------

# NUMERIC(38,10): PostgreSQL 11. basamagi SESSIZCE yuvarlar (olculdu),
# bu yuzden yuvarlama Python tarafinda bilincli yapilir (S5.7).
FACT_QUANTUM = Decimal("1E-10")


def to_fact_value(value: Any) -> Decimal | None:
    """FactValueType() = DECIMAL(38,10) icin degeri quantize eder.

    `localcontext(prec=FACT_PRECISION)` ZORUNLUDUR: Python'un varsayilan
    context'i 28 ANLAMLI hane tasir, kolon ise 38 (28 tam + 10 ondalik).
    Varsayilanla 1e18 `InvalidOperation` firlatir -- kolon 1e28'e kadar
    kabul ederken. Istisna `normalize` icinden cikip `runner`'a ulasir ve o
    (sembol x dataset) hucresinin TUM satirlarini dusururdu: tek bir buyuk
    degerin bedeli donemin butun kalemleri olurdu. prec=38 ile Python siniri
    kolonun gercek sinirina esitlenir; GERCEKTEN tasan deger satiri dusurur
    ve hucre `ok` kalir (`key_value` deseni).
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
    """Sentinel bos dize -> NULL (AH S8.3).

    Kaynak `ToGrade`, `Position`, `Transaction`, `URL`, `priceTargetAction`
    alanlarinda "deger yok"u `''` ile bildiriyor. Duz `to_str` bunu bos bir
    dize olarak yazar ve "bos" ile "bilinmiyor" ayrimi kaybolurdu.
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
    """PK bilesenine giren metin; sinirdan uzunsa None + WARNING (AH S8.3).

    KIRPILMAZ: kirpilmis bir anahtar iki FARKLI kaydi tek satirda birlestirir
    ve bu sessiz bir veri kaybidir. Satiri dusurmek F S8.4'un desenidir --
    tek bozuk anahtar yuzunden ayni cagrinin gecerli satirlarini da
    kaybetmemek icin hucre `failed` yapilmaz, kayip loga yazilir.
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
    """`date_range="filter"` elemesi (AH S6.2).

    Aralik verilmemisse (None/None) hicbir satir elenmez: filtresiz
    calistirma Yahoo'nun verdigi TUM gecmisi yazar.
    """
    if value is None:
        return False
    if start is not None and value < start:
        return False
    return not (end is not None and value > end)


def to_big_value(value: Any) -> Any:
    """BigNumType() = DECIMAL(38,0); kesirli deger Python tarafinda yuvarlanir.

    PostgreSQL kesirli kismi SESSIZCE yuvarlar (olculdu:
    olculdu); yuvarlamayi burada yapmak `kinds.py`'nin `big` kuralini
    tek dogruluk kaynagi olarak korur.
    """
    return KINDS["big"].convert(value)


def to_datetime_value(value: Any) -> Any:
    """TsType() kolonu; kaynak `datetime64` VEYA ham epoch `float64` verir.

    `kinds.py`'nin `dt` kurali iki bicimi de kabul eder; `insider_roster`in
    Position Direct/Indirect Date alanlari sembole gore ikisi arasinda
    degisiyor (6 sembolde dolu float olculdu).
    """
    return KINDS["dt"].convert(value)


def date_range_kwargs(start: date | None, end: date | None) -> dict[str, str]:
    """`date_range="api"` dataset'lerinin yfinance cagri argumanlari.

    Alt sinir ACIKCA konur: `start=None` ile cagrildiginda yfinance kendi
    varsayilan penceresini uygular (`get_shares_full` icin 'end - 548 gun')
    ve yalnizca `--end` verilmis bir calistirma sessizce daralirdi.

    Ust sinir BIR GUN ILERI tasinir: Yahoo `period2`yi DISLAYICI okur;
    `--end 2018-12-31` o gunu KAPSAMALIDIR.
    """
    kwargs = {"start": (start or EPOCH_START).isoformat()}
    if end is not None:
        kwargs["end"] = (end + timedelta(days=1)).isoformat()
    return kwargs


# --- kesif dataset'lerinin ortak yardimcilari (SQ denetimi) ---------------
# Uc kesif dataset'i (`search`, `lookup`, `screener`) ayni dort isi
# yapiyordu ve dordu de UC KEZ kopyalanmisti. `domain/common.py` ayni
# durumu ayni bicimde cozuyor.


def symbol_is_writable(symbol: str) -> bool:
    """Sembol `symbols` tablosuna yazilabilir mi (SQ S8.3).

    Kisit `SymbolType()` = VARCHAR(SYMBOL_LENGTH) COLLATE "C"den TURETILIR;
    uzunluk burada sabit olarak yazilmaz.

    `^` KAPSAM ICINDEDIR: olculen 9.243 sembolun 93'u onunla basliyor
    (endeksler). Karakter kumesini daraltan bir dogrulama endeksleri
    toptan reddederdi.

    Yazma SIRASINDAN turetilmez, `normalize` icinde hesaplanir: siraya
    bagli bir turetme kapi kapsami degistiginde sessizce bozulurdu
    (SQ S6.2.1).
    """
    from yfin.models.base import SYMBOL_LENGTH

    return len(symbol) <= SYMBOL_LENGTH and symbol.isascii()


def utc_as_of_day(fetched_at: datetime) -> date:
    """`as_of_date`i CEKIM DAMGASINDAN turetir, `now()`tan degil.

    Uc dataset de `datetime.now(UTC).date()` cagiriyordu; o durumda kapi
    satirinin `as_of_date`i ile `fetched_at`i FARKLI zaman kaynaklarindan
    gelir ve gece yarisi gecisinde ayrisir -- satirlar 5 Eylul damgasiyla
    6 Eylul gunune yazilabilirdi. `domain/common.as_of_day` ayni ilkeyi
    piyasa saat dilimi icin uyguluyor; kesif tarafi bolge-bagimsiz oldugu
    icin UTC kullanir.
    """
    moment = fetched_at if fetched_at.tzinfo is not None else fetched_at.replace(tzinfo=UTC)
    return moment.astimezone(UTC).date()


def expect_dict(value: Any, *, what: str) -> dict[str, Any]:
    """Yanit sozluk degilse YUKSEK SESLE patlar.

    Sessizce bos donmek "veri yok" (`empty`) ile "yanit sekli degisti"
    (`failed`) durumlarini birbirine karistirirdi; ikincisi acilen
    gorulmesi gereken bir seydir.
    """
    if not isinstance(value, dict):
        raise TypeError(f"{what} yaniti sozluk degil: {type(value).__name__}")
    return value


def dict_items(payload: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    """`payload[key]` listesindeki SOZLUK ogeleri; digerleri elenir.

    Kaynak bir blokta beklenmedik bir skaler dondurdugunde tek satir
    dusmeli, hucrenin tamami degil.
    """
    return [item for item in payload.get(key) or [] if isinstance(item, dict)]


def discovered_symbol_row(
    symbol: str,
    *,
    source: str,
    fetched_at: datetime,
    **typed_fields: Any,
) -> dict[str, Any]:
    """Kesfedilen sembolun `symbols` satiri (SQ K10).

    Dort ortak alan BURADA, tek yerde durur. Kaynaga ozgu tanimlayici
    alanlar `typed_fields` ile gecer -- her yol yalnizca GERCEKTEN
    doldurdugunu verir ve `update_columns` demeti de ona gore dar tutulur
    (SQ S5.12).

    `is_active`, `discovered_by` ve `discovered_at` YALNIZ INSERT'te
    etkilidir: uc yolun da `update_columns` demeti bunlari DISLAR. Kapsama
    girselerdi operatorun elle aktiflestirdigi bir sembol, ertesi gun
    yeniden kesfedildiginde SESSIZCE pasife donerdi.
    """
    return {
        "symbol": symbol,
        **typed_fields,
        "is_active": False,
        "discovered_by": source,
        "discovered_at": fetched_at,
        "last_seen_at": fetched_at,
    }
