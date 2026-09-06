"""Builds a SQLAlchemy column from a Field definition."""

from __future__ import annotations

from typing import Any

from sqlalchemy import CheckConstraint, Column

from yfin.models.fields import Field
from yfin.models.kinds import KINDS


def make_column(field: Field, table_name: str) -> Column[Any]:
    """Typed column; all of them are nullable because the set of fields a
    source returns varies per symbol.

    If the kind declares a CHECK (KindSpec.check) the constraint is added.
    This function does not know which kinds are constrained -- if it did,
    adding a constrained kind would mean changing this file too.

    The constraint is named explicitly, which is why `table_name` is
    passed in. A column-level CheckConstraint cannot resolve
    `column_0_name` in the naming convention: every constraint on a table
    would come out as `ck_<table>_` and PostgreSQL rejects the duplicate.
    Field carries no table name, and both callers (snapshots.py,
    discovery.py) already know theirs.
    """
    spec = KINDS[field.kind]
    args: list[Any] = [field.column, spec.sql_type()]
    if spec.check is not None:
        args.append(
            CheckConstraint(
                spec.check(field.column),
                name=f"ck_{table_name}_{field.column}_nonneg",
            )
        )
    return Column(*args, nullable=True)
