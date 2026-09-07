"""Decoding Yahoo's pricing protobuf.

The first test is the important one: it fails when upstream adds a field
to pricing.proto, which is the only way a new field can be noticed before
it is silently dropped.
"""

from __future__ import annotations

import base64
import json
import math
import struct
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from yfinance.pricing_pb2 import PricingData

from yfin.models.base import SYMBOL_LENGTH
from yfin.stream import protocol as pr


def _message(**fields: object) -> PricingData:
    message = PricingData()
    for name, value in fields.items():
        setattr(message, name, value)
    return message


def _decoded(**fields: object) -> dict:
    result = pr.decode_pricing_data(_message(id="AAPL", time=1_700_000_000_000, **fields))
    assert result.row is not None
    return result.row


# --- the schema gate -------------------------------------------------------


def test_every_proto_field_is_mapped() -> None:
    """A new upstream field breaks the build instead of vanishing.

    Without this, adding a field to pricing.proto would mean the value
    lands in `unknown_fields` at best and is lost at worst -- and nobody
    would find out until someone asked why a column is empty.
    """
    proto_fields = {f.name for f in PricingData.DESCRIPTOR.fields}
    assert proto_fields == set(pr.FIELD_COLUMNS), (
        f"unmapped: {proto_fields - set(pr.FIELD_COLUMNS)}, "
        f"stale: {set(pr.FIELD_COLUMNS) - proto_fields}"
    )


def test_proto_has_the_thirty_three_fields_the_design_assumes() -> None:
    assert len(PricingData.DESCRIPTOR.fields) == 33


def test_column_names_are_unique() -> None:
    """Two proto fields mapping to one column would silently overwrite."""
    columns = list(pr.FIELD_COLUMNS.values())
    assert len(columns) == len(set(columns))


# --- float32 ---------------------------------------------------------------


@pytest.mark.parametrize("text", ["232.35", "0.0001", "1e12", "3.4028235e38", "-17.25"])
def test_f32_decimal_round_trips(text: str) -> None:
    widened = struct.unpack("<f", struct.pack("<f", float(text)))[0]
    assert float(pr.f32_decimal(widened)) == pytest.approx(widened)
    back = struct.unpack("<f", struct.pack("<f", float(pr.f32_decimal(widened))))[0]
    assert back == widened


def test_f32_decimal_drops_the_widening_artefact() -> None:
    """The whole point: 232.35 must not be stored as 232.35000610351562."""
    widened = struct.unpack("<f", struct.pack("<f", 232.35))[0]
    assert widened != 232.35  # protobuf really does hand us this
    assert pr.f32_decimal(widened) == Decimal("232.35")


def _shortest_round_trip(value: float) -> str:
    """Independently computed shortest form, to check f32_decimal against."""
    for digits in range(1, 10):
        candidate = f"{value:.{digits}g}"
        try:
            if struct.unpack("<f", struct.pack("<f", float(candidate)))[0] == value:
                return candidate
        except OverflowError:
            continue
    raise AssertionError(f"no round-trip within 9 digits for {value!r}")


@pytest.mark.parametrize("raw", [232.35, 0.1, 1234.5678, 99.99, 3.4028235e38, 1e-38])
def test_f32_decimal_is_the_shortest_form(raw: float) -> None:
    """Not merely *a* round-trip: the shortest one, computed separately."""
    widened = struct.unpack("<f", struct.pack("<f", raw))[0]
    assert pr.f32_decimal(widened) == Decimal(_shortest_round_trip(widened))


def test_price_column_uses_the_shortest_decimal() -> None:
    row = _decoded(price=232.35)
    assert row["price"] == Decimal("232.35")


# --- presence --------------------------------------------------------------


def test_absent_fields_become_null() -> None:
    row = _decoded(price=100.0)
    assert row["bid"] is None
    assert row["ask"] is None
    assert row["day_volume"] is None
    assert row["short_name"] is None


def test_market_hours_zero_is_stored_not_nulled() -> None:
    """PRE_MARKET is code 0, so the generic presence rule would blank it.

    is_extended is derived from this column and price_bars.is_extended is
    NOT NULL -- nulling it would break exactly the pre-market rows that
    need the flag most.
    """
    row = _decoded(price=1.0)
    assert row["market_hours_code"] == pr.MARKET_HOURS_PRE
    assert row["quote_type_code"] == 0


def test_market_hours_regular_is_not_extended() -> None:
    assert pr.is_extended_session(pr.MARKET_HOURS_REGULAR) is False
    assert pr.is_extended_session(pr.MARKET_HOURS_PRE) is True
    assert pr.is_extended_session(pr.MARKET_HOURS_POST) is True


def test_explicit_zero_and_absent_are_indistinguishable() -> None:
    """Documents a protocol limit rather than a decision.

    proto3 gives scalars no presence, so a bid of exactly 0.0 is the same
    bytes as no bid at all. Consumers must read NULL as "absent or zero".
    """
    assert _decoded(bid=0.0)["bid"] is None


# --- symbol ----------------------------------------------------------------


def test_symbol_is_normalised() -> None:
    result = pr.decode_pricing_data(_message(id="aapl", time=1_700_000_000_000))
    assert result.row is not None
    assert result.row["symbol"] == "AAPL"


def test_symbol_is_trimmed_of_whitespace() -> None:
    result = pr.decode_pricing_data(_message(id="  MSFT  ", time=1_700_000_000_000))
    assert result.row is not None
    assert result.row["symbol"] == "MSFT"


def test_empty_symbol_is_rejected() -> None:
    result = pr.decode_pricing_data(_message(id="", time=1_700_000_000_000))
    assert result.row is None
    assert [r.reason for r in result.rejects] == [pr.REJECT_UNKNOWN_SYMBOL]


def test_over_long_symbol_is_rejected_not_truncated() -> None:
    """Truncating would file the tick under a different symbol.

    There is no raw_json column to recover from, so a silent trim is
    unrecoverable corruption.
    """
    result = pr.decode_pricing_data(
        _message(id="A" * (SYMBOL_LENGTH + 5), time=1_700_000_000_000)
    )
    assert result.row is None
    assert [r.reason for r in result.rejects] == [pr.REJECT_SYMBOL_TOO_LONG]


def test_over_long_underlying_nulls_only_that_field() -> None:
    """The row still carries a valid tick; only the extra field goes."""
    row = _decoded(price=1.0, underlying_symbol="U" * (SYMBOL_LENGTH + 1))
    assert row["underlying_symbol"] is None
    assert row["price"] == Decimal("1")


# --- time ------------------------------------------------------------------


def test_time_is_milliseconds() -> None:
    row = _decoded(price=1.0)
    assert row["ts_utc"] == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)


@pytest.mark.parametrize("millis", [0, -1])
def test_missing_timestamp_rejects_the_row(millis: int) -> None:
    """ts_utc is a key component; received_at is not a substitute."""
    result = pr.decode_pricing_data(_message(id="AAPL", time=millis))
    assert result.row is None
    assert [r.reason for r in result.rejects] == [pr.REJECT_NO_TIMESTAMP]


def test_expire_date_is_seconds_not_milliseconds() -> None:
    """The two timestamp fields disagree on units, on purpose."""
    row = _decoded(price=1.0, expire_date=1_700_000_000)
    assert row["expire_date_utc"] == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)


def test_expire_date_out_of_range_is_nulled_with_a_reject() -> None:
    """A millisecond value here would land in the year 55000."""
    result = pr.decode_pricing_data(
        _message(id="AAPL", time=1_700_000_000_000, expire_date=1_700_000_000_000)
    )
    assert result.row is not None
    assert result.row["expire_date_utc"] is None
    assert [r.reason for r in result.rejects] == [pr.REJECT_EXPIRE_DATE_RANGE]


# --- bad values ------------------------------------------------------------


def test_non_finite_price_nulls_the_field_and_keeps_the_row() -> None:
    """One bad field must not cost the other 32."""
    result = pr.decode_pricing_data(
        _message(id="AAPL", time=1_700_000_000_000, price=math.nan, bid=5.0)
    )
    assert result.row is not None
    assert result.row["price"] is None
    assert result.row["bid"] == Decimal("5")
    assert [r.reason for r in result.rejects] == [pr.REJECT_NON_FINITE]


def test_out_of_range_int_nulls_the_field_and_keeps_the_row() -> None:
    """These are sint64 upstream but INTEGER here; one odd value must not
    abort a 500-row batch with a DataError."""
    result = pr.decode_pricing_data(
        _message(id="AAPL", time=1_700_000_000_000, price_hint=2**40)
    )
    assert result.row is not None
    assert result.row["price_hint_code"] is None
    assert [r.reason for r in result.rejects] == [pr.REJECT_OUT_OF_RANGE]


def test_bigint_fields_are_not_narrowed() -> None:
    """day_volume and friends are BIGINT, so large values pass through."""
    row = _decoded(day_volume=2**40)
    assert row["day_volume"] == 2**40


# --- payload_hash ----------------------------------------------------------


def test_payload_hash_is_stable_across_calls() -> None:
    first = _decoded(price=1.0, bid=2.0)
    second = _decoded(price=1.0, bid=2.0)
    assert first["payload_hash"] == second["payload_hash"]


def test_payload_hash_ignores_received_at() -> None:
    """Otherwise the same message re-sent would never dedupe."""
    early = pr.decode_pricing_data(
        _message(id="AAPL", time=1_700_000_000_000, price=1.0),
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    late = pr.decode_pricing_data(
        _message(id="AAPL", time=1_700_000_000_000, price=1.0),
        received_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    assert early.row is not None and late.row is not None
    assert early.row["payload_hash"] == late.row["payload_hash"]


def test_payload_hash_changes_with_any_field() -> None:
    assert _decoded(price=1.0)["payload_hash"] != _decoded(price=1.01)["payload_hash"]


def test_payload_hash_has_the_column_width() -> None:
    assert len(_decoded(price=1.0)["payload_hash"]) == 16


# --- unknown fields --------------------------------------------------------


def test_unknown_fields_is_null_when_everything_is_mapped() -> None:
    assert _decoded(price=1.0)["unknown_fields"] is None


def test_row_carries_every_column_the_table_has() -> None:
    """A missing key would make the COPY batch ragged."""
    row = _decoded(price=1.0)
    expected = set(pr.FIELD_COLUMNS.values()) | set(pr.DERIVED_COLUMNS)
    assert set(row) == expected


# --- envelope --------------------------------------------------------------


def _envelope(**fields: object) -> str:
    message = _message(id="BTC-USD", time=1_700_000_000_000, **fields)
    encoded = base64.b64encode(message.SerializeToString()).decode()
    return json.dumps({"type": "pricing", "message": encoded})


def test_envelope_round_trip() -> None:
    result = pr.decode_envelope(_envelope(price=232.35))
    assert result.row is not None
    assert result.row["symbol"] == "BTC-USD"
    assert result.row["price"] == Decimal("232.35")
    assert result.rejects == []


def test_envelope_accepts_bytes() -> None:
    assert pr.decode_envelope(_envelope(price=1.0).encode()).row is not None


def test_envelope_without_type_is_accepted() -> None:
    """`type` is optional; only a *wrong* type is a problem."""
    message = _message(id="BTC-USD", time=1_700_000_000_000, price=1.0)
    raw = json.dumps({"message": base64.b64encode(message.SerializeToString()).decode()})
    assert pr.decode_envelope(raw).row is not None


def test_unexpected_envelope_type_is_rejected() -> None:
    """yfinance ignores `type` and hands the parser an empty string,
    producing a silently empty record. We refuse instead."""
    result = pr.decode_envelope(json.dumps({"type": "status", "message": "AAAA"}))
    assert result.row is None
    assert result.rejects[0].reason == pr.REJECT_DECODE_FAILED


@pytest.mark.parametrize(
    "raw",
    [
        "hello world",
        "[1, 2, 3]",
        json.dumps({"type": "pricing"}),
        json.dumps({"type": "pricing", "message": ""}),
        json.dumps({"type": "pricing", "message": "not base64!!"}),
    ],
)
def test_malformed_envelopes_are_rejected(raw: str) -> None:
    result = pr.decode_envelope(raw)
    assert result.row is None
    assert result.rejects[0].reason == pr.REJECT_DECODE_FAILED


def test_decode_failure_keeps_the_wire_bytes() -> None:
    """live_ticks has no raw_json, so this is the only surviving evidence."""
    result = pr.decode_envelope("hello world")
    assert result.rejects[0].raw_base64 is not None


def test_rejected_row_carries_the_wire_bytes() -> None:
    message = _message(id="", time=1_700_000_000_000)
    encoded = base64.b64encode(message.SerializeToString()).decode()
    result = pr.decode_envelope(json.dumps({"type": "pricing", "message": encoded}))
    assert result.row is None
    assert result.rejects[0].raw_base64 == encoded


# --- subscription safety ---------------------------------------------------


def test_validate_subscription_normalises_and_keeps_order() -> None:
    clean, rejects = pr.validate_subscription(["aapl", "msft"])
    assert clean == ["AAPL", "MSFT"]
    assert rejects == []


def test_validate_subscription_drops_none() -> None:
    """Measured: `{"subscribe": [null]}` closes the socket with no status
    code. One None would take down every symbol on the connection."""
    clean, rejects = pr.validate_subscription(["AAPL", None, "MSFT"])  # type: ignore[list-item]
    assert clean == ["AAPL", "MSFT"]
    assert [r.reason for r in rejects] == [pr.REJECT_MALFORMED_SUBSCRIPTION]


def test_validate_subscription_drops_blank_and_over_long() -> None:
    clean, rejects = pr.validate_subscription(["", "   ", "A" * 40, "OK"])
    assert clean == ["OK"]
    assert [r.reason for r in rejects] == [
        pr.REJECT_MALFORMED_SUBSCRIPTION,
        pr.REJECT_MALFORMED_SUBSCRIPTION,
        pr.REJECT_SYMBOL_TOO_LONG,
    ]
