"""Paging and serialisation rules shared by every v1 route.

These lived in both routers as identical copies, which is exactly how a
page-size cap or a cursor rule ends up meaning two different things
depending on which endpoint you ask. There is one copy now, and it is in
the router layer because refusing a request is an HTTP decision -- the
storage layer stays free of it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import Request

from yfin.api.core.errors import TYPE_INVALID_CURSOR, TYPE_INVALID_PARAMETER, ApiProblem
from yfin.api.schemas.common import DEFAULT_PAGE_SIZE
from yfin.api.storage import cursor as cursors


def page_size(request: Request, requested: int | None) -> int:
    """The effective page size, refusing rather than silently clipping.

    Quietly returning fewer rows than asked for is indistinguishable, from
    the caller's side, from reaching the end of the data.
    """
    cap = int(getattr(request.state, "page_size_cap", DEFAULT_PAGE_SIZE))
    if requested is None:
        return min(DEFAULT_PAGE_SIZE, cap)
    if requested > cap:
        raise ApiProblem(
            422,
            TYPE_INVALID_PARAMETER,
            "Page size above the plan's maximum",
            detail=f"limit must not exceed {cap}",
        )
    return requested


def decode_cursor(
    cursor: str | None, *, query: dict[str, Any], arity: int
) -> tuple[Any, ...] | None:
    """Turns every cursor failure into a 422, never a 500."""
    if cursor is None:
        return None
    try:
        return cursors.decode(cursor, query=query, arity=arity)
    except cursors.InvalidCursor as exc:
        raise ApiProblem(
            422, TYPE_INVALID_CURSOR, "The cursor is not usable here", detail=str(exc)
        ) from exc


def to_number(value: Decimal | None) -> str | None:
    """Decimals cross the wire as strings.

    Prices are Numeric(28,12) and large counts Numeric(38,0) precisely so
    they are not floats. Serialising them as JSON numbers would undo that
    at the API boundary, where it is least visible and most permanent.
    """
    return None if value is None else format(value, "f")


def serialise_row(row: dict[str, Any]) -> dict[str, Any]:
    """Row serialisation for the generic surface, which has no hand-written
    schema to apply `to_number` field by field."""
    return {
        key: (to_number(value) if isinstance(value, Decimal) else value)
        for key, value in row.items()
    }
