"""Normalization rules.

Every rule here is based on a live-API observation; simplifying any of
them reintroduces a concrete, previously-observed bug.
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

from yfin.core.logging_setup import get_logger

log = get_logger(__name__)

# Sentinel values meaning "no data" in the source
SENTINELS: frozenset[str] = frozenset({"-", "", "N/A", "n/a", "None", "null"})

# Epoch field map. Units are never guessed.
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

# Fields whose name suggests epoch but that aren't converted
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

# Range for the "looks like epoch" warning on unmapped fields:
# 1990-01-01 .. 2100-01-01 (seconds)
_EPOCH_LOW = 631_152_000
_EPOCH_HIGH = 4_102_444_800


def normalize_symbol(symbol: str) -> str:
    """One canonical form. With COLLATE "C" this makes 'aapl'/'AAPL'
    collisions impossible."""
    return symbol.strip().upper()


def is_missing(value: Any) -> bool:
    """None / NaN / NaT / pd.NA / sentinel string -> missing."""
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
        # Truncation isn't data loss (the full text stays in raw_json), but
        # doing it silently hides column-width decisions.
        log.debug("value truncated", max_len=max_len, original_len=len(text))
        text = text[:max_len]
    return text


def to_int(value: Any) -> int | None:
    """Converts to a Python int, including numpy integer types."""
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
    """float -> DECIMAL conversion.

    ``Decimal(repr(float(x)))`` is required:
    - Bare ``Decimal(repr(x))`` breaks on numpy 2.x, since
      ``repr(np.float64(0.00187))`` == ``'np.float64(0.00187)'``.
    - ``Decimal(float(x))`` produces binary residue (0.001870000000000000041...).
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
    """Converts a tz-aware value to UTC; treats a naive value as UTC.

    Returns a UTC-aware value. The PostgreSQL column is `timestamptz` and
    retains tz info.
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
        # A naive value is treated as UTC (documented contract) and marked
        # explicit; otherwise a naive value would go into the `timestamptz`
        # column and psycopg would interpret it by connection TZ -- the
        # result comes out right but the type inconsistency persists.
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    return None


def to_local_date(value: Any) -> date | None:
    """The exchange's local session date.

    Converting to UTC first and taking the date shifts it back a day for
    positive-offset exchanges (BIST, Tokyo): THYAO 2000-05-10 00:00+03:00
    -> 2000-05-09 21:00 UTC. So no tz conversion happens here.
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
    """Epoch -> UTC-aware datetime. unit is 's' or 'ms'."""
    raw = to_int(value)
    if raw is None:
        return None
    seconds = raw / 1000.0 if unit == "ms" else float(raw)
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def warn_unmapped_epoch_like(payload: Mapping[str, Any], mapped_keys: frozenset[str]) -> list[str]:
    """Warns about fields not in the map that fall in the epoch range."""
    suspects: list[str] = []
    for key, value in payload.items():
        if key in mapped_keys or key in NOT_EPOCH_FIELDS:
            continue
        if isinstance(value, bool | np.bool_):
            continue
        if isinstance(value, int | np.integer):
            number = int(value)
        elif isinstance(value, float | np.floating) and float(value).is_integer():
            # Yahoo can return an epoch as a float (1704067200.0); scanning
            # ints only would silently miss this field.
            number = int(value)
        else:
            continue
        if _EPOCH_LOW <= number <= _EPOCH_HIGH:
            suspects.append(key)
    for key in suspects:
        log.warning("unmapped epoch-like key", key=key)
    return suspects


def is_empty_result(raw: Any) -> bool:
    """Empty-result check.

    ``.empty`` alone isn't enough: ``get_shares_full`` can return None,
    and ``None.empty`` raises AttributeError.
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
    """Encoder for raw_json.

    ``HistoryMetadata`` is a Mapping, not a dict; ``tradingPeriods`` is a
    DataFrame. Plain ``json.dumps`` raises TypeError on these.
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
    """Converts NaN/Inf to None so allow_nan=False doesn't raise."""
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
    """Canonical JSON.

    sort_keys + allow_nan=False + ensure_ascii=False + compact separators.

    allow_nan=False is required, and the reason hasn't changed even though
    the database engine has: MySQL's JSON type used to reject a body
    containing NaN (ERROR 3140), rolling back the whole symbol's
    transaction. The column is TEXT now, so NaN would be written silently
    -- and since `NaN != NaN`, comparing via `content_hash` would open the
    hash gate on every run, rewriting unchanged data forever. So this
    function, not the storage engine, is responsible for the gate.
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
    """SHA-256 over canonical JSON. Always computed in Python."""
    import hashlib

    text = canonical if canonical is not None else canonical_json(payload)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def as_mapping(raw: Any) -> dict[str, Any]:
    """Safely converts a source object to a dict.

    HistoryMetadata is a Mapping, not a dict -- and violates the Mapping
    contract itself: keys() lists 'tradingPeriods' but __getitem__ raises
    KeyError for that same key (yfinance/scrapers/history.py:55). Observed
    on mutual funds (VFIAX), where a plain dict(raw) call turned the whole
    symbol into unknown_symbol. So keys are read one by one, and an
    unreadable key is skipped.
    """
    if isinstance(raw, dict):
        return raw

    out: dict[str, Any] = {}
    unreadable: list[str] = []
    # .keys() is deliberate: source objects aren't dicts, and __iter__
    # isn't guaranteed
    for key in raw.keys():  # noqa: SIM118
        try:
            out[str(key)] = raw[key]
        except (KeyError, AttributeError, TypeError):
            unreadable.append(str(key))
    if unreadable:
        log.warning("unreadable source keys", keys=unreadable)
    return out


def normalize_person_name(name: str) -> str:
    """company_officers.name: the source has double spaces
    ('Mr. Kevan  Parekh'); without normalizing, duplicate rows result."""
    return " ".join(name.split())
