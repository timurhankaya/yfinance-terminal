"""Type-layer invariants over the type factories and the ENTIRE `Base.metadata`: a policy
defined in one helper does not protect a table written without it. No database."""

from __future__ import annotations

from sqlalchemy import Enum, String, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP

from yfin.models import Base
from yfin.models.base import NAMING_CONVENTION, RawJsonType, TsType


def test_timestamps_are_timestamptz_with_microseconds() -> None:
    """TIMESTAMP(6) WITH TIME ZONE via the dialect type (generic `TIMESTAMP` rejects
    `precision`). Six digits: second precision would collide on the (symbol, fetched_at) PK
    of ticker_info_history, and PostgreSQL rounds the fraction rather than truncating."""
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
    """Every length-bound String/VARCHAR column must carry COLLATE "C", or the database
    default (en_US.utf8) applies. Covers all three sources: models/base.py factories,
    models/kinds.py `strN` kinds, and direct `String(n)` calls in model files. `Enum` is
    excluded: it derives from `String` in SQLAlchemy but takes no collation in PostgreSQL."""
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
