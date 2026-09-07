"""How a stored column arrives on the wire.

The wire type, not the SQL type: a caller does not care that a price is
Numeric(28,12), only that it arrives as a string it must not parse as a
float.

This lives under `storage/` rather than in `api/` because two documents
describe the same columns and must not disagree about them: the API's
dataset catalogue, and the change-event schema, which describes rows the
pipeline publishes for tables the API may not serve at all. A second copy
of the mapping would be a second answer to one question.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Column
from sqlalchemy import types as sqltypes


# Order matters -- Enum and VARCHAR are subclasses of String, TIMESTAMP of
# DateTime -- so the checks run most specific first.
def wire_type(column: Column[Any]) -> str:
    """The JSON shape of one column, or a refusal.

    An unknown type raises, and it raises at import like every other bad
    declaration in the catalogue. The alternative -- emitting "unknown", or
    500ing on the first request to /v1/datasets -- would publish a column
    shape nobody had checked. Today the exposed tables use eleven types and
    none of them is JSONB, ARRAY or bytea; the first one that is should stop
    the process, not reach a client.
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
