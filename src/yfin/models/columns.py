"""Builds a SQLAlchemy column from a Field definition."""

from __future__ import annotations

from typing import Any

from sqlalchemy import CheckConstraint, Column

from yfin.models.fields import Field
from yfin.models.kinds import KINDS


def make_column(field: Field, table_name: str) -> Column[Any]:
    """Typed, nullable column (the fields a source returns vary per symbol),
    plus the kind's CHECK if KindSpec.check is set. `table_name` names the
    constraint explicitly: a column-level CheckConstraint cannot resolve
    `column_0_name` in the naming convention, so every constraint would
    come out as `ck_<table>_` and collide."""
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
