"""`xid8` has no cast from bigint and psycopg has no loader for it: a bound parameter must
be cast explicitly in the SQL, and psycopg 3 returns the value as text. `Xid8Type` holds
both halves; the round trip is in `tests/repo/test_changes_schema_repo.py`."""

from __future__ import annotations

import sqlalchemy as sa

from yfin.models.base import Xid8Type


def _compiled(value: object) -> str:
    """The SQL a bound parameter of this type compiles to."""
    table = sa.Table("probe", sa.MetaData(), sa.Column("xid", Xid8Type()))
    statement = table.insert().values(xid=value)
    return str(statement.compile(dialect=sa.dialects.postgresql.dialect()))


def test_the_column_type_is_xid8() -> None:
    assert Xid8Type().get_col_spec() == "xid8"


def test_a_bound_parameter_is_cast_in_the_sql() -> None:
    """PostgreSQL has no `bigint -> xid8` cast, so without this the
    parameter arrives as an integer and the statement fails."""
    assert "CAST(" in _compiled(12345)
    assert "AS xid8" in _compiled(12345)


def test_binding_goes_through_str() -> None:
    """psycopg has no adapter for xid8, so the value travels as text."""
    processor = Xid8Type().bind_processor(sa.dialects.postgresql.dialect())
    assert processor is not None
    assert processor(12345) == "12345"


def test_binding_leaves_none_alone() -> None:
    processor = Xid8Type().bind_processor(sa.dialects.postgresql.dialect())
    assert processor is not None
    assert processor(None) is None


def test_a_returned_value_becomes_an_int() -> None:
    """The relay compares cursors and orders by them; text ordering would
    put '9' after '10'."""
    processor = Xid8Type().result_processor(sa.dialects.postgresql.dialect(), None)
    assert processor is not None
    assert processor("78967") == 78967
    assert isinstance(processor("78967"), int)


def test_a_returned_null_stays_none() -> None:
    processor = Xid8Type().result_processor(sa.dialects.postgresql.dialect(), None)
    assert processor is not None
    assert processor(None) is None
