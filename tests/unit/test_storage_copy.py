"""COPY text encoding and the JSON rendering that rides along with it: one encoder shared by
the stream writer and the change outbox. Stream batching stays in `test_stream_writer.py`."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from yfin.storage.copy import copy_body, copy_value, jsonable

TS = datetime(2026, 9, 7, 14, 30, tzinfo=UTC)


def _row(symbol: str = "AAPL", **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "symbol": symbol,
        "ts_utc": TS,
        "price": Decimal("232.35"),
        "bid": Decimal("232.30"),
        "unknown_fields": None,
    }
    row.update(overrides)
    return row


def test_copy_body_writes_one_line_per_row() -> None:
    body = copy_body([_row("AAPL"), _row("MSFT")], ("symbol",))
    assert body.count("\n") == 2


def test_copy_body_uses_the_column_order_it_is_given() -> None:
    """The caller passes the schema's order, so it cannot drift from the
    table -- and two tables with different orders can share the encoder."""
    body = copy_body([_row()], ("symbol", "ts_utc"))
    assert body.startswith("AAPL\t2026-09-07 14:30:00+00:00")


def test_copy_body_encodes_null() -> None:
    body = copy_body([_row(bid=None)], ("symbol", "bid"))
    assert body == "AAPL\t\\N\n"


def test_copy_body_encodes_a_missing_key_as_null() -> None:
    """A column the row omits is NULL, not a KeyError: `align_rows` and the
    stream's optional fields both rely on it."""
    assert copy_body([{"symbol": "AAPL"}], ("symbol", "bid")) == "AAPL\t\\N\n"


def test_copy_body_keeps_decimal_precision() -> None:
    """The whole point of f32_decimal would be lost to a float repr here."""
    body = copy_body([_row(price=Decimal("232.35"))], ("price",))
    assert body == "232.35\n"


def test_copy_body_escapes_structural_characters() -> None:
    """`unknown_fields` is upstream JSON: a tab or newline in it would
    otherwise shift every following column by one."""
    body = copy_body([_row(unknown_fields='{"a":"x\ty"}')], ("symbol", "unknown_fields"))
    assert body == 'AAPL\t{"a":"x\\ty"}\n'
    assert body.count("\t") == 1  # the separator, not the payload


def test_copy_body_escapes_backslashes() -> None:
    body = copy_body([_row(unknown_fields="a\\b")], ("unknown_fields",))
    assert body == "a\\\\b\n"


def test_copy_value_encodes_booleans_the_way_postgres_reads_them() -> None:
    assert copy_value(True) == "t"
    assert copy_value(False) == "f"


def test_jsonable_renders_decimals_as_text() -> None:
    """A float would reintroduce the f32 artefact for every consumer."""
    assert jsonable(Decimal("232.35")) == "232.35"


def test_jsonable_renders_datetimes_as_text() -> None:
    assert jsonable(TS) == "2026-09-07 14:30:00+00:00"


def test_jsonable_passes_json_native_values_through() -> None:
    for value in (None, True, 1, 1.5, "AAPL"):
        assert jsonable(value) is value
