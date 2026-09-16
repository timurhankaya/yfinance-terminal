"""Yahoo's pricing protobuf -> a row for `live_ticks`.

Price fields are binary32 on the wire (`f32_decimal` undoes the widening).
proto3 scalars have no presence: absent means NULL, except MEANINGFUL_ZERO.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import struct
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from yfinance.pricing_pb2 import PricingData

from yfin.core import normalize as nz
from yfin.models.base import SYMBOL_LENGTH
from yfin.stream.rejects import (
    REJECT_DECODE_FAILED,
    REJECT_EXPIRE_DATE_RANGE,
    REJECT_MALFORMED_SUBSCRIPTION,
    REJECT_NO_TIMESTAMP,
    REJECT_NON_FINITE,
    REJECT_OUT_OF_RANGE,
    REJECT_SYMBOL_TOO_LONG,
    REJECT_UNKNOWN_SYMBOL,
    DecodeResult,
    Reject,
)

#: The only envelope type observed on the wire. yfinance never checks it.
ENVELOPE_TYPE_PRICING: Final = "pricing"

# --- enum codes ------------------------------------------------------------
# Not in pricing.proto (both fields are a bare int32 there); values come
# from Yahoo's yaticker.proto. A Python constant, not a database enum, so
# there is no second copy to drift.

MARKET_HOURS_PRE: Final = 0
MARKET_HOURS_REGULAR: Final = 1
MARKET_HOURS_POST: Final = 2
MARKET_HOURS_EXTENDED: Final = 3

MARKET_HOURS_NAMES: Final[dict[int, str]] = {
    MARKET_HOURS_PRE: "pre_market",
    MARKET_HOURS_REGULAR: "regular_market",
    MARKET_HOURS_POST: "post_market",
    MARKET_HOURS_EXTENDED: "extended_hours_market",
}

QUOTE_TYPE_NAMES: Final[dict[int, str]] = {
    0: "none", 5: "altsymbol", 7: "heartbeat", 8: "equity", 9: "index",
    11: "mutualfund", 12: "moneymarket", 13: "option", 14: "currency",
    15: "warrant", 17: "bond", 18: "future", 20: "etf", 23: "commodity",
    28: "ecnquote", 41: "cryptocurrency", 42: "indicator", 1000: "industry",
}


def is_extended_session(market_hours_code: int) -> bool:
    """Anything that is not the regular session counts as extended.

    Classifies the code Yahoo sent, which may differ from the market's
    real state. Fills `price_bars.is_extended` (NOT NULL).
    """
    return market_hours_code != MARKET_HOURS_REGULAR


# --- field mapping ---------------------------------------------------------
#
# One entry per field in pricing.proto. test_stream_protocol checks this
# against PricingData.DESCRIPTOR.fields, so a new upstream field breaks
# the build instead of being dropped on the floor.

#: proto float (binary32) -> NUMERIC(28,12) via f32_decimal.
PRICE_FIELDS: Final[tuple[str, ...]] = (
    "price", "change_percent", "day_high", "day_low", "change",
    "open_price", "previous_close", "strike_price", "bid", "ask",
)

#: proto sint64 -> BIGINT.
BIGINT_FIELDS: Final[tuple[str, ...]] = (
    "day_volume", "open_interest", "last_size", "bid_size", "ask_size",
    "vol_24hr", "vol_all_currencies",
)

#: proto sint64 that is really a small code -> INTEGER.
#
# INTEGER, not SMALLINT: these are sint64 on the wire, and narrowing them
# to +/-32767 would let one out-of-range value abort the whole batch with
# a DataError. A batch must never be lost because of one odd field.
INT_FIELDS: Final[tuple[str, ...]] = ("options_type", "mini_option", "price_hint")

#: proto string -> VARCHAR, with the width the column actually has.
TEXT_FIELDS: Final[dict[str, int]] = {
    "currency": 32,
    "exchange": 32,
    "short_name": 128,
    "from_currency": 32,
    "last_market": 64,
}

#: proto double -> NUMERIC. No f32 correction: these are already binary64.
DOUBLE_FIELDS: Final[tuple[str, ...]] = ("circulating_supply", "market_cap")

#: Fields whose zero value is a real value, so the presence rule does not
#: apply. Both are Yahoo enum codes where 0 is a legitimate member
#: (market_hours 0 = PRE_MARKET, quote_type 0 = NONE); the columns are
#: NOT NULL and absent means 0.
MEANINGFUL_ZERO: Final[dict[str, str]] = {
    "quote_type": "quote_type_code",
    "market_hours": "market_hours_code",
}

#: Fields handled by hand because the column name or the unit differs.
SPECIAL_FIELDS: Final[dict[str, str]] = {
    "id": "symbol",
    "time": "ts_utc",
    "expire_date": "expire_date_utc",
    "underlying_symbol": "underlying_symbol",
}

#: Every proto field this module knows about -> its column.
FIELD_COLUMNS: Final[dict[str, str]] = {
    **{name: name for name in PRICE_FIELDS},
    **{name: name for name in BIGINT_FIELDS},
    **{name: f"{name}_code" for name in INT_FIELDS},
    **{name: name for name in TEXT_FIELDS},
    **{name: name for name in DOUBLE_FIELDS},
    **MEANINGFUL_ZERO,
    **SPECIAL_FIELDS,
}

#: Columns of live_ticks that do not come from a proto field.
DERIVED_COLUMNS: Final[tuple[str, ...]] = (
    "payload_hash", "received_at", "unknown_fields",
)

#: PostgreSQL INTEGER bounds. Values outside are stored as NULL with a
#: field_out_of_range reject rather than aborting the insert.
_INT_MIN: Final = -2_147_483_648
_INT_MAX: Final = 2_147_483_647

#: expire_date is seconds since epoch (time is MILLIseconds -- the two
#: fields disagree on units). Anything outside 1970..2100 is treated as a
#: unit mix-up rather than a date and dropped.
_EXPIRE_MIN: Final = 0
_EXPIRE_MAX: Final = 4_102_444_800  # 2100-01-01


def f32_decimal(value: float) -> Decimal:
    """The shortest decimal that round-trips back to the same float32.

    binary32 round-trips in at most 9 significant digits. Near the ceiling
    a rounded-up candidate overflows `struct.pack`; it is not a round-trip.
    """
    for digits in range(1, 10):
        text = f"{value:.{digits}g}"
        try:
            if struct.unpack("<f", struct.pack("<f", float(text)))[0] == value:
                return Decimal(text)
        except OverflowError:
            continue
    return Decimal(repr(value))


def _present_fields(message: PricingData) -> set[str]:
    """Fields whose value is not the type default; proto3 scalars have no presence."""
    return {descriptor.name for descriptor, _ in message.ListFields()}


def _canonical_payload(row: dict[str, Any]) -> str:
    """Stable text for payload_hash, a primary key component: sorted keys, str() values."""
    return json.dumps(
        {k: (str(v) if isinstance(v, Decimal | datetime) else v) for k, v in sorted(row.items())},
        separators=(",", ":"),
        ensure_ascii=False,
    )


def payload_hash(row: dict[str, Any]) -> str:
    """First 16 hex digits of the SHA-256 over the mapped fields.

    Includes unknown_fields, or a message differing only in a new proto
    field would be discarded by ON CONFLICT DO NOTHING.
    """
    payload = {
        k: v for k, v in row.items() if k not in ("payload_hash", "received_at")
    }
    digest = hashlib.sha256(_canonical_payload(payload).encode("utf-8")).hexdigest()
    return digest[:16]


def _decode_symbol(message: PricingData, rejects: list[Reject]) -> str | None:
    """`id` -> a symbol that can be compared against `symbols`.

    SymbolType() is COLLATE "C", so case folding must happen here. Over-long
    symbols are rejected, not truncated, or the tick changes symbol.
    """
    raw = message.id or ""
    symbol = nz.normalize_symbol(raw)
    if not symbol:
        rejects.append(Reject(REJECT_UNKNOWN_SYMBOL, detail="empty id"))
        return None
    if len(symbol) > SYMBOL_LENGTH:
        rejects.append(
            Reject(REJECT_SYMBOL_TOO_LONG, symbol=symbol[:SYMBOL_LENGTH],
                   detail=f"len={len(symbol)}")
        )
        return None
    return symbol


def _decode_timestamp(
    message: PricingData, symbol: str, rejects: list[Reject]
) -> datetime | None:
    """`time` is MILLIseconds since epoch; missing means no row, as ts_utc is part of the key."""
    millis = int(message.time)
    if millis <= 0:
        rejects.append(Reject(REJECT_NO_TIMESTAMP, symbol=symbol, detail=str(millis)))
        return None
    return datetime.fromtimestamp(millis / 1000, UTC)


def _decode_expire_date(
    message: PricingData, symbol: str, present: set[str], rejects: list[Reject]
) -> datetime | None:
    """`expire_date` is SECONDS since epoch, unlike `time`.

    Anything outside 1970..2100 is treated as a unit mix-up and dropped.
    """
    if "expire_date" not in present:
        return None
    seconds = int(message.expire_date)
    if not _EXPIRE_MIN <= seconds <= _EXPIRE_MAX:
        rejects.append(
            Reject(REJECT_EXPIRE_DATE_RANGE, symbol=symbol, detail=str(seconds))
        )
        return None
    return datetime.fromtimestamp(seconds, UTC)


def _decode_price(
    message: PricingData, name: str, symbol: str, rejects: list[Reject]
) -> Decimal | None:
    value = float(getattr(message, name))
    if not math.isfinite(value):
        rejects.append(Reject(REJECT_NON_FINITE, symbol=symbol, detail=name))
        return None
    return f32_decimal(value)


def _decode_double(
    message: PricingData, name: str, symbol: str, rejects: list[Reject]
) -> Decimal | None:
    value = float(getattr(message, name))
    if not math.isfinite(value):
        rejects.append(Reject(REJECT_NON_FINITE, symbol=symbol, detail=name))
        return None
    return Decimal(repr(value))


def _decode_bounded_int(
    message: PricingData, name: str, symbol: str, rejects: list[Reject]
) -> int | None:
    value = int(getattr(message, name))
    if not _INT_MIN <= value <= _INT_MAX:
        rejects.append(Reject(REJECT_OUT_OF_RANGE, symbol=symbol, detail=f"{name}={value}"))
        return None
    return value


def decode_pricing_data(
    message: PricingData, *, received_at: datetime | None = None
) -> DecodeResult:
    """One PricingData -> one live_ticks row.

    Row is None when the message cannot be keyed; any other problem nulls
    a single column and keeps the row.
    """
    rejects: list[Reject] = []
    now = received_at or datetime.now(UTC)

    symbol = _decode_symbol(message, rejects)
    if symbol is None:
        return DecodeResult(rejects=rejects)

    ts_utc = _decode_timestamp(message, symbol, rejects)
    if ts_utc is None:
        return DecodeResult(rejects=rejects)

    present = _present_fields(message)
    row: dict[str, Any] = {"symbol": symbol, "ts_utc": ts_utc}

    for name in PRICE_FIELDS:
        row[name] = _decode_price(message, name, symbol, rejects) if name in present else None
    for name in DOUBLE_FIELDS:
        row[name] = _decode_double(message, name, symbol, rejects) if name in present else None
    for name in BIGINT_FIELDS:
        row[name] = int(getattr(message, name)) if name in present else None
    for name in INT_FIELDS:
        row[FIELD_COLUMNS[name]] = (
            _decode_bounded_int(message, name, symbol, rejects) if name in present else None
        )
    for name, width in TEXT_FIELDS.items():
        row[name] = nz.to_str(getattr(message, name), width) if name in present else None

    # The presence rule does not apply here: 0 is a real code, and the
    # columns are NOT NULL.
    for name, column in MEANINGFUL_ZERO.items():
        row[column] = int(getattr(message, name))

    # No FK on this one: an option's underlying may well be outside the
    # universe, and refusing the tick over it would lose real data. Too
    # long means null the field, not drop the row.
    underlying = nz.normalize_symbol(message.underlying_symbol or "")
    row["underlying_symbol"] = underlying if 0 < len(underlying) <= SYMBOL_LENGTH else None

    row["expire_date_utc"] = _decode_expire_date(message, symbol, present, rejects)

    # Fields upstream added that this module does not map yet. Normally
    # NULL, so it costs nothing; when it is not NULL it is the only
    # evidence that pricing.proto moved.
    unknown = sorted(present - set(FIELD_COLUMNS))
    row["unknown_fields"] = (
        json.dumps({name: str(getattr(message, name)) for name in unknown},
                   separators=(",", ":"), ensure_ascii=False)
        if unknown
        else None
    )

    row["payload_hash"] = payload_hash(row)
    row["received_at"] = now
    return DecodeResult(row=row, rejects=rejects)


def decode_envelope(raw: str | bytes, *, received_at: datetime | None = None) -> DecodeResult:
    """One websocket frame (JSON wrapping a base64 protobuf) -> a live_ticks row.

    Every failure carries the original base64: with no raw_json column,
    `stream_rejects.raw_base64` is the only evidence of what arrived.
    """
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    try:
        envelope = json.loads(text)
    except (ValueError, TypeError) as exc:
        return DecodeResult(rejects=[
            Reject(REJECT_DECODE_FAILED, detail=f"not json: {exc}", raw_base64=text[:512])
        ])
    if not isinstance(envelope, dict):
        return DecodeResult(rejects=[
            Reject(REJECT_DECODE_FAILED, detail=f"envelope is {type(envelope).__name__}",
                   raw_base64=text[:512])
        ])

    kind = envelope.get("type")
    if kind is not None and kind != ENVELOPE_TYPE_PRICING:
        return DecodeResult(rejects=[
            Reject(REJECT_DECODE_FAILED, detail=f"unexpected type: {kind!r}",
                   raw_base64=text[:512])
        ])

    encoded = envelope.get("message")
    if not encoded or not isinstance(encoded, str):
        return DecodeResult(rejects=[
            Reject(REJECT_DECODE_FAILED, detail=f"no message field: {sorted(envelope)[:5]}",
                   raw_base64=text[:512])
        ])

    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        return DecodeResult(rejects=[
            Reject(REJECT_DECODE_FAILED, detail=f"not base64: {exc}", raw_base64=encoded)
        ])

    message = PricingData()
    try:
        message.ParseFromString(payload)
    except Exception as exc:  # protobuf raises DecodeError, not a stdlib type
        return DecodeResult(rejects=[
            Reject(REJECT_DECODE_FAILED, detail=f"not protobuf: {exc}", raw_base64=encoded)
        ])

    result = decode_pricing_data(message, received_at=received_at)
    # Carry the wire bytes onto decode-time rejects so an operator can
    # replay exactly what Yahoo sent.
    if result.row is None and result.rejects:
        result.rejects = [
            Reject(r.reason, r.symbol, r.detail, encoded) for r in result.rejects
        ]
    return result


def validate_subscription(symbols: list[str]) -> tuple[list[str], list[Reject]]:
    """Filters a subscription list down to what is safe to send.

    One malformed entry closes the socket silently, taking down every
    symbol on that connection, so validation happens before sending.
    """
    clean: list[str] = []
    rejects: list[Reject] = []
    for entry in symbols:
        if not isinstance(entry, str):
            rejects.append(Reject(REJECT_MALFORMED_SUBSCRIPTION,
                                  detail=f"{type(entry).__name__}: {entry!r}"))
            continue
        symbol = nz.normalize_symbol(entry)
        if not symbol:
            rejects.append(Reject(REJECT_MALFORMED_SUBSCRIPTION, detail="empty"))
            continue
        if len(symbol) > SYMBOL_LENGTH:
            rejects.append(Reject(REJECT_SYMBOL_TOO_LONG, symbol=symbol[:SYMBOL_LENGTH],
                                  detail=f"len={len(symbol)}"))
            continue
        clean.append(symbol)
    return clean, rejects
