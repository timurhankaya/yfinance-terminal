"""PostgreSQL's COPY text format, and the JSON rendering that feeds it.

Written for the stream writer, which measured COPY at 22,291 ticks/s
against 6,219 for `INSERT ... ON CONFLICT` (docs/measurements/websocket.md).
It lives under `storage/` rather than in `stream/` because the pipeline's
change outbox writes the same way, and `storage/` may not import `stream/`.

Nothing here touches a session: these turn rows into a string, and the
caller hands that string to psycopg's `copy`.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

_COPY_NULL: Final = r"\N"


def copy_value(value: Any) -> str:
    """One field in PostgreSQL's COPY text format."""
    if value is None:
        return _COPY_NULL
    if isinstance(value, Decimal | datetime):
        return str(value)
    if isinstance(value, bool):
        return "t" if value else "f"
    text_value = str(value)
    # Escape what the text format treats as structure. Symbols and currency
    # codes never contain these, but upstream JSON could.
    return (
        text_value.replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def jsonable(value: Any) -> Any:
    """Decimal and datetime as text, so the payload round-trips exactly.

    A float here would undo f32_decimal for every consumer downstream --
    the artefact this pipeline exists to remove would be reintroduced on the
    way out.
    """
    if isinstance(value, Decimal | datetime):
        return str(value)
    return value


def copy_body(rows: Sequence[dict[str, Any]], columns: Sequence[str]) -> str:
    """Renders rows as a COPY payload, in the given column order."""
    buffer = io.StringIO()
    for row in rows:
        buffer.write("\t".join(copy_value(row.get(name)) for name in columns))
        buffer.write("\n")
    return buffer.getvalue()


__all__ = ["copy_body", "copy_value", "jsonable"]
