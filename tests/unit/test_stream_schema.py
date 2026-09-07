"""The stream schema against the decoder that fills it.

`protocol.py` produces a dict; `models/stream.py` declares the columns it
lands in. Nothing in Python links the two, so without these tests a
renamed column would only fail at write time -- against a real database,
in production, at the end of a batch.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Boolean, Integer, SmallInteger

from yfin.models import Base
from yfin.models.stream import (
    LiveQuote,
    LiveTick,
    StreamRejectReason,
    stream_timescale_ddl,
)
from yfin.stream import protocol as pr

STREAM_TABLES = (
    "live_ticks",
    "live_quotes",
    "stream_scope",
    "stream_outbox",
    "stream_relay_offset",
    "stream_rejects",
    "stream_sessions",
    "stream_connection_health",
)


# --- decoder <-> schema ----------------------------------------------------


def test_live_ticks_has_exactly_the_columns_the_decoder_produces() -> None:
    """The decoder's dict is written verbatim; a mismatch is a failed write."""
    decoded = set(pr.FIELD_COLUMNS.values()) | set(pr.DERIVED_COLUMNS)
    assert set(LiveTick.__table__.c.keys()) == decoded


def test_live_quotes_carries_the_same_measurement_as_live_ticks() -> None:
    """The two are one measurement seen twice; a column in only one is a bug."""
    ticks = set(LiveTick.__table__.c.keys())
    quotes = set(LiveQuote.__table__.c.keys())
    assert quotes - ticks == {"updated_at"}
    assert ticks - quotes == set()


def test_reject_reasons_match_the_decoder_constants() -> None:
    """The enum is the storage side of protocol.py's REJECT_* constants."""
    from_protocol = {
        value
        for name, value in vars(pr).items()
        if name.startswith("REJECT_") and isinstance(value, str)
    }
    assert {r.value for r in StreamRejectReason} == from_protocol


# --- keys ------------------------------------------------------------------


def test_live_ticks_primary_key_is_the_dedupe_triple() -> None:
    """Drop any one of these and data is lost.

    Without payload_hash a re-sent snapshot would collide; without ts_utc
    two ticks in the same millisecond would collapse into one.
    """
    assert [c.name for c in LiveTick.__table__.primary_key] == [
        "symbol",
        "ts_utc",
        "payload_hash",
    ]


def test_live_quotes_is_keyed_on_symbol_alone() -> None:
    assert [c.name for c in LiveQuote.__table__.primary_key] == ["symbol"]


def test_hypertable_keys_contain_the_partitioning_column() -> None:
    """TimescaleDB rejects a unique index that does not include it.

    Getting this wrong fails at CREATE time with "cannot create a unique
    index without the column ... used in partitioning".
    """
    partitioning = {"live_ticks": "ts_utc", "stream_outbox": "created_at"}
    for table_name, column in partitioning.items():
        table = Base.metadata.tables[table_name]
        assert column in {c.name for c in table.primary_key}


# --- nullability that other code depends on --------------------------------


@pytest.mark.parametrize("column", ["quote_type_code", "market_hours_code"])
def test_enum_code_columns_are_not_null(column: str) -> None:
    """The exception to the presence rule has to hold in the schema too.

    market_hours 0 is PRE_MARKET. If this column were nullable the
    decoder's exception would be pointless and price_bars.is_extended
    (NOT NULL) could not be derived for pre-market rows.
    """
    assert LiveTick.__table__.c[column].nullable is False


def test_small_code_columns_are_wide_enough() -> None:
    """options_type/mini_option/price_hint are sint64 upstream.

    SMALLINT would let one out-of-range value abort a whole batch with a
    DataError; the point of INTEGER here is that a batch never dies over
    one odd field.
    """
    for name in ("options_type_code", "mini_option_code", "price_hint_code"):
        column_type = LiveTick.__table__.c[name].type
        assert isinstance(column_type, Integer)
        assert not isinstance(column_type, SmallInteger)


def test_received_at_is_not_null() -> None:
    """It is the only end-to-end lag signal; a NULL would blind `status`."""
    assert LiveTick.__table__.c["received_at"].nullable is False


# --- foreign keys ----------------------------------------------------------


def test_tables_that_must_carry_a_symbol_fk_do() -> None:
    for table_name in ("live_ticks", "live_quotes", "stream_scope"):
        table = Base.metadata.tables[table_name]
        parents = {fk.column.table.name for fk in table.c["symbol"].foreign_keys}
        assert parents == {"symbols"}, table_name


def test_reject_symbol_has_no_fk() -> None:
    """Mandatory: an unknown_symbol reject is about a symbol not in
    `symbols`. With an FK the record of the rejection would itself be
    rejected."""
    assert not Base.metadata.tables["stream_rejects"].c["symbol"].foreign_keys


def test_outbox_symbol_has_no_fk() -> None:
    """Different reason: everything here already passed the known-symbol
    filter. The FK is skipped for cost -- a shared lock on `symbols` per
    insert, on a transient queue."""
    assert not Base.metadata.tables["stream_outbox"].c["symbol"].foreign_keys


def test_underlying_symbol_has_no_fk() -> None:
    """An option's underlying may be outside the universe."""
    assert not LiveTick.__table__.c["underlying_symbol"].foreign_keys


# --- scope -----------------------------------------------------------------


def test_scope_defaults_to_archiving() -> None:
    scope = Base.metadata.tables["stream_scope"]
    for name in ("enabled", "archive"):
        column = scope.c[name]
        assert isinstance(column.type, Boolean)
        assert column.nullable is False
        assert column.server_default is not None


# --- hypertable DDL --------------------------------------------------------


def test_stream_ddl_covers_both_hypertables() -> None:
    ddl = " ".join(stream_timescale_ddl())
    assert "live_ticks" in ddl
    assert "stream_outbox" in ddl


def test_stream_ddl_disables_default_indexes() -> None:
    """Without this, autogenerate reports the default index as a deletion
    forever and the empty-diff gate never opens again."""
    for statement in stream_timescale_ddl():
        assert "create_default_indexes => FALSE" in statement


def test_stream_ddl_is_not_merged_into_the_bars_ddl() -> None:
    """The initial migration calls the bars function against a schema
    where these tables do not exist; merging would break a past
    migration."""
    from yfin.models.bars import timescale_ddl

    assert "live_ticks" not in " ".join(timescale_ddl())


def test_all_tables_are_registered() -> None:
    for name in STREAM_TABLES:
        assert name in Base.metadata.tables
