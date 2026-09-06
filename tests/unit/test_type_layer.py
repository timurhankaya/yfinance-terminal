"""Type-layer invariants. Does not touch a database.

These tests look at the type factories and the ENTIRE `Base.metadata`, not
individual tables. Defining a policy in one helper does not protect it -- a
new table can be written without using the helper and silently drift.
"""

from __future__ import annotations

from sqlalchemy import Enum, String, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP

from yfin.models import Base
from yfin.models.base import NAMING_CONVENTION, RawJsonType, TsType


def test_timestamps_are_timestamptz_with_microseconds() -> None:
    """DATETIME(6) -> TIMESTAMP(6) WITH TIME ZONE.

    The generic `sqlalchemy.TIMESTAMP` rejects `precision` (TypeError); the
    dialect type is required. Six digits are mandatory: second precision
    would collide on the (symbol, fetched_at) PK of ticker_info_history,
    and PostgreSQL rounds the fraction rather than truncating it.
    """
    ts = TsType()
    assert isinstance(ts, TIMESTAMP)
    assert ts.timezone is True
    assert ts.precision == 6


def test_raw_json_is_plain_text() -> None:
    """Not JSONB: it reorders keys, rejects NaN, and normalizes numbers --
    all three would break content_hash."""
    assert isinstance(RawJsonType(), Text)


def test_metadata_has_naming_convention() -> None:
    """Without a naming convention, Alembic generates unstable names for
    unnamed constraints and `yfin db revision` reports a fake diff on every
    call -- the "empty diff" gate never opens."""
    assert Base.metadata.naming_convention == NAMING_CONVENTION
    assert "ck" in NAMING_CONVENTION


def test_no_string_column_lacks_c_collation() -> None:
    """Every length-bound String/VARCHAR column must carry COLLATE "C".

    Plain `String(n)` columns took the TABLE default (utf8mb4_0900_ai_ci)
    in MySQL; in PostgreSQL the DATABASE default (en_US.utf8) applies
    instead -- neither "C" nor the old behavior.

    There are three sources and all three must be covered:
      1. models/base.py factories (SymbolType, AsciiKeyType, ...)
      2. models/kinds.py `strN` kinds (_c_string)
      3. `String(n)` calls written directly in model files
    The third was missed in the first pass; this test exists because of it.

    `Enum` is excluded: in SQLAlchemy, `Enum` derives from `String`, but in
    PostgreSQL it is a separate type (CREATE TYPE) and takes no collation.
    """
    offenders = [
        f"{table.name}.{col.name}"
        for table in Base.metadata.tables.values()
        for col in table.columns
        if isinstance(col.type, String)
        and not isinstance(col.type, Enum)
        and col.type.length is not None
        and getattr(col.type, "collation", None) != "C"
    ]
    assert not offenders, f"VARCHAR without collation ({len(offenders)}): {offenders}"


def test_row_size_budget_helpers_are_gone() -> None:
    """PostgreSQL has no 65,535-byte row size limit: large values move to
    TOAST. The practical limit is the column count (1,600)."""
    import yfin.models.columns as columns

    assert not hasattr(columns, "MYSQL_ROW_SIZE_LIMIT")
    assert not hasattr(columns, "estimated_row_size")


def test_mysql_table_args_is_gone() -> None:
    import yfin.models.base as base

    assert not hasattr(base, "MYSQL_TABLE_ARGS")
