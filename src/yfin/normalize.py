"""Normalizasyon kurallari (tasarim dokumani S8.3 ve S8.4).

Buradaki her kural canli API gozlemine dayanir; sadelestirme girisimleri
dokumanda listelenen somut hatalari geri getirir.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

from yfin.logging_setup import get_logger

log = get_logger(__name__)

# S8.3: kaynakta "veri yok" anlamina gelen sentinel degerler
SENTINELS: frozenset[str] = frozenset({"-", "", "N/A", "n/a", "None", "null"})

# S8.4: epoch alan haritasi. Birim tahmin edilmez.
EPOCH_MS_FIELDS: frozenset[str] = frozenset({"firstTradeDateMilliseconds"})

EPOCH_SEC_FIELDS: frozenset[str] = frozenset(
    {
        "exDividendDate",
        "dividendDate",
        "lastDividendDate",
        "earningsTimestamp",
        "earningsTimestampStart",
        "earningsTimestampEnd",
        "earningsCallTimestampStart",
        "earningsCallTimestampEnd",
        "lastFiscalYearEnd",
        "nextFiscalYearEnd",
        "mostRecentQuarter",
        "lastSplitDate",
        "governanceEpochDate",
        "compensationAsOfEpochDate",
        "dateShortInterest",
        "sharesShortPreviousMonthDate",
        "preMarketTime",
        "regularMarketTime",
        "fundInceptionDate",
        "startDate",
    }
)

# Adi epoch cagristirsa da donusturulmeyen alanlar (S8.4)
NOT_EPOCH_FIELDS: frozenset[str] = frozenset(
    {
        "fullTimeEmployees",
        "allTimeHigh",
        "allTimeLow",
        "isEarningsDateEstimate",
        "exchangeTimezoneName",
        "exchangeTimezoneShortName",
    }
)

# Haritalanmamis alanlarda "epoch gibi duruyor" uyarisi icin aralik:
# 1990-01-01 .. 2100-01-01 (saniye)
_EPOCH_LOW = 631_152_000
_EPOCH_HIGH = 4_102_444_800


def normalize_symbol(symbol: str) -> str:
    """Tek kanonik bicim (S8.3). COLLATE "C" ile birlikte
    'aapl'/'AAPL' karisikligini imkansiz kilar."""
    return symbol.strip().upper()


def is_missing(value: Any) -> bool:
    """None / NaN / NaT / pd.NA / sentinel string -> eksik."""
    if value is None or value is pd.NaT or value is pd.NA:
        return True
    if isinstance(value, str):
        return value.strip() in SENTINELS
    if isinstance(value, float | np.floating):
        return bool(math.isnan(float(value)) or math.isinf(float(value)))
    if isinstance(value, Decimal):
        return value.is_nan()
    return False


def to_str(value: Any, max_len: int | None = None) -> str | None:
    if is_missing(value):
        return None
    text = str(value).strip()
    if text in SENTINELS:
        return None
    if max_len is not None and len(text) > max_len:
        # Kirpma veri kaybi degildir (tam metin raw_json'da durur) ama
        # sessiz kalmasi kolon genisligi kararlarini gorunmez kilar.
        log.debug("value truncated", max_len=max_len, original_len=len(text))
        text = text[:max_len]
    return text


def to_int(value: Any) -> int | None:
    """numpy tamsayilari dahil Python int'e cevirir."""
    if is_missing(value):
        return None
    if isinstance(value, bool | np.bool_):
        return int(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float | np.floating):
        return int(float(value))
    if isinstance(value, Decimal):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def to_bool(value: Any) -> bool | None:
    if is_missing(value):
        return None
    if isinstance(value, bool | np.bool_):
        return bool(value)
    if isinstance(value, int | float | np.integer | np.floating):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "yes", "1"}:
        return True
    if text in {"false", "no", "0"}:
        return False
    return None


def to_decimal(value: Any) -> Decimal | None:
    """float -> DECIMAL donusumu (S8.3).

    ``Decimal(repr(float(x)))`` zorunludur:
    - Ciplak ``Decimal(repr(x))`` numpy 2.x'te patlar, cunku
      ``repr(np.float64(0.00187))`` == ``'np.float64(0.00187)'``.
    - ``Decimal(float(x))`` ise ikili artik uretir (0.001870000000000000041...).
    """
    if is_missing(value):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | np.integer) and not isinstance(value, bool | np.bool_):
        return Decimal(int(value))
    try:
        return Decimal(repr(float(value)))
    except (TypeError, ValueError, ArithmeticError):
        return None


def to_datetime_utc(value: Any) -> datetime | None:
    """tz-aware degeri UTC'ye cevirir, naive degeri UTC KABUL EDER.

    Donen deger UTC-AWARE'dir. MySQL DATETIME(6) tz tasimadigi icin damga
    naive'e indiriliyordu; PostgreSQL kolonu `timestamptz`tir ve tz
    bilgisini SAKLAR (PG S2.3).
    """
    if is_missing(value):
        return None
    if isinstance(value, str):
        try:
            value = pd.Timestamp(value)
        except (TypeError, ValueError):
            return None
    if isinstance(value, pd.Timestamp):
        ts = value.tz_convert(UTC) if value.tzinfo is not None else value.tz_localize(UTC)
        aware: datetime = ts.to_pydatetime()
        return aware
    if isinstance(value, datetime):
        # Naive deger UTC KABUL EDILIR (dokumante edilmis sozlesme) ve
        # acikca isaretlenir; aksi halde `timestamptz` kolonuna naive
        # deger giderdi ve psycopg onu baglanti TZ'sine gore yorumlardi --
        # sonuc dogru cikar ama tip tutarsizligi kalicilasir.
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    return None


def to_local_date(value: Any) -> date | None:
    """Borsanin YEREL seans tarihi (S5.4).

    UTC'ye cevirip tarih almak pozitif ofsetli borsalarda (BIST, Tokyo)
    tarihi bir gun geri kaydirir: THYAO 2000-05-10 00:00+03:00 ->
    2000-05-09 21:00 UTC. Bu yuzden tz donusumu YAPILMAZ.
    """
    if is_missing(value):
        return None
    if isinstance(value, str):
        try:
            value = pd.Timestamp(value)
        except (TypeError, ValueError):
            return None
    if isinstance(value, pd.Timestamp):
        day: date = value.date()
        return day
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def epoch_to_datetime(value: Any, *, unit: str = "s") -> datetime | None:
    """Epoch -> UTC-AWARE datetime. unit 's' veya 'ms'."""
    raw = to_int(value)
    if raw is None:
        return None
    seconds = raw / 1000.0 if unit == "ms" else float(raw)
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def convert_epoch_field(key: str, value: Any) -> datetime | None:
    """Alan adina gore dogru birimle cozer (S8.4)."""
    if key in EPOCH_MS_FIELDS:
        return epoch_to_datetime(value, unit="ms")
    if key in EPOCH_SEC_FIELDS:
        return epoch_to_datetime(value, unit="s")
    raise KeyError(f"epoch haritasinda yok: {key}")


def warn_unmapped_epoch_like(payload: Mapping[str, Any], mapped_keys: frozenset[str]) -> list[str]:
    """Haritada olmayip epoch araliginda gorunen alanlari uyarir (S8.4)."""
    suspects: list[str] = []
    for key, value in payload.items():
        if key in mapped_keys or key in NOT_EPOCH_FIELDS:
            continue
        if isinstance(value, bool | np.bool_):
            continue
        if isinstance(value, int | np.integer):
            number = int(value)
        elif isinstance(value, float | np.floating) and float(value).is_integer():
            # Yahoo bir epoch'u float olarak dondurebilir (1704067200.0);
            # yalnizca int taransaydi bu alan sessizce kacardi.
            number = int(value)
        else:
            continue
        if _EPOCH_LOW <= number <= _EPOCH_HIGH:
            suspects.append(key)
    for key in suspects:
        log.warning("unmapped epoch-like key", key=key)
    return suspects


def is_empty_result(raw: Any) -> bool:
    """Bos sonuc kontrolu (S8.3).

    ``.empty`` tek basina yetmez: ``get_shares_full`` None donebilir ve
    ``None.empty`` AttributeError verir.
    """
    if raw is None:
        return True
    if isinstance(raw, pd.DataFrame | pd.Series):
        return bool(raw.empty)
    if isinstance(raw, Mapping | Sequence):
        return len(raw) == 0
    try:
        return len(raw) == 0
    except TypeError:
        return False


class YFJSONEncoder(json.JSONEncoder):
    """raw_json icin encoder (S8.3).

    ``HistoryMetadata`` bir dict degil, Mapping'dir; ``tradingPeriods`` bir
    DataFrame'dir. Duz ``json.dumps`` TypeError verir.
    """

    def default(self, o: Any) -> Any:
        if isinstance(o, pd.DataFrame):
            return json.loads(o.reset_index().to_json(orient="records", date_format="iso"))
        if isinstance(o, pd.Series):
            return json.loads(o.to_json(orient="index", date_format="iso"))
        if isinstance(o, pd.Timestamp | datetime):
            return o.isoformat()
        if isinstance(o, date):
            return o.isoformat()
        if isinstance(o, Decimal):
            return str(o)
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            return float(o)
        if isinstance(o, np.bool_):
            return bool(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
        if isinstance(o, Mapping):
            return dict(o)
        if isinstance(o, set | frozenset):
            return sorted(str(x) for x in o)
        return str(o)


def _scrub_nan(value: Any) -> Any:
    """NaN/Inf degerlerini None'a cevirir; allow_nan=False'in patlamamasi icin."""
    if isinstance(value, Mapping):
        return {str(k): _scrub_nan(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_scrub_nan(v) for v in value]
    if isinstance(value, pd.DataFrame | pd.Series):
        return value
    if isinstance(value, float | np.floating) and not math.isfinite(float(value)):
        return None
    if value is pd.NaT or value is pd.NA:
        return None
    return value


def canonical_json(payload: Any) -> str:
    """Kanonik JSON (S7.2).

    sort_keys + allow_nan=False + ensure_ascii=False + kompakt ayiricilar.

    allow_nan=False ZORUNLUDUR. Gerekce motor degisimiyle DEGISMEDI,
    yalnizca belirtisi degisti: MySQL JSON tipi NaN iceren govdeyi
    reddediyordu (ERROR 3140) ve tum sembolun transaction'i geri
    alinirdi. Kolon artik TEXT oldugu icin (PG S2.4) NaN sessizce
    YAZILIRDI -- ve `content_hash` uzerinden karsilastirildiginda
    `NaN != NaN` oldugu icin hash kapisi HER KOSUDA acilir, degismeyen
    veri surekli yeniden yazilirdi. Yani kapi artik motorun degil BU
    fonksiyonun sorumlulugundadir.
    """
    return json.dumps(
        _scrub_nan(payload),
        sort_keys=True,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        cls=YFJSONEncoder,
    )


def content_hash(payload: Any | None = None, *, canonical: str | None = None) -> str:
    """Kanonik JSON uzerinden SHA-256 (S7.2). Her zaman Python tarafinda."""
    import hashlib

    text = canonical if canonical is not None else canonical_json(payload)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def as_mapping(raw: Any) -> dict[str, Any]:
    """Kaynak nesnesini guvenle dict'e cevirir.

    HistoryMetadata bir dict DEGIL, Mapping'dir (S8.3) - ve Mapping
    sozlesmesini de ihlal eder: keys() 'tradingPeriods' anahtarini
    listeler ama __getitem__ ayni anahtar icin KeyError firlatir
    (yfinance/scrapers/history.py:55). Bu, yatirim fonlarinda (VFIAX)
    gozlendi ve duz dict(raw) cagrisi tum sembolu unknown_symbol yapiyordu.
    Bu yuzden anahtarlar tek tek okunur, cozulemeyen anahtar atlanir.
    """
    if isinstance(raw, dict):
        return raw

    out: dict[str, Any] = {}
    unreadable: list[str] = []
    # .keys() bilincli: kaynak nesneler dict degil ve __iter__ garantisi yok
    for key in raw.keys():  # noqa: SIM118
        try:
            out[str(key)] = raw[key]
        except (KeyError, AttributeError, TypeError):
            unreadable.append(str(key))
    if unreadable:
        log.warning("unreadable source keys", keys=unreadable)
    return out


def normalize_person_name(name: str) -> str:
    """company_officers.name (S8.3): kaynakta cift bosluk var
    ('Mr. Kevan  Parekh'); normalize edilmezse duplike satir olusur."""
    return " ".join(name.split())
