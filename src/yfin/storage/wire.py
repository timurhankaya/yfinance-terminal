"""How a stored column arrives on the wire.

Under `storage/`, not `api/`: the API's dataset catalogue and the
change-event schema describe the same columns and must not disagree.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Column
from sqlalchemy import types as sqltypes


# Order matters -- Enum and VARCHAR are subclasses of String, TIMESTAMP of
# DateTime -- so the checks run most specific first.
def wire_type(column: Column[Any]) -> str:
    """The JSON shape of one column, or a refusal.

    An unknown type raises at import, so an unchecked column shape never reaches a client.
    """
    kind = column.type
    if isinstance(kind, sqltypes.Boolean):
        return "boolean"
    if isinstance(kind, sqltypes.Numeric) and not isinstance(kind, sqltypes.Float):
        # Serialised by `paging.to_number`, which is why it is a string.
        return "string (decimal)"
    if isinstance(kind, sqltypes.DateTime):
        return "string (date-time)"
    if isinstance(kind, sqltypes.Date):
        return "string (date)"
    if isinstance(kind, sqltypes.Integer):
        return "integer"
    if isinstance(kind, sqltypes.String):
        # Covers Enum and Text: an enum column arrives as its value.
        return "string"
    raise ValueError(
        f"{column.table.name}.{column.name}: {type(kind).__name__} has no published "
        "wire type; teach storage/wire.wire_type about it before exposing it"
    )


__all__ = ["wire_type"]
