"""The generic dataset surface: catalogue and data.

Two endpoints stand in for dozens. The scope a request needs is not fixed
per route here -- it depends on which dataset is asked for -- so the
guard cannot be declared in the signature the way the hand-written
endpoints declare theirs. It is resolved from the catalogue and applied
inside the handler instead, in the same order the rest of the API uses:
authenticate, then scope, then existence.

That ordering matters more here than anywhere else. Dataset names are the
one part of the surface a caller could enumerate, and reading 404 against
403 is exactly how they would do it.

OpenAPI describes one path with one parameter shape rather than a schema
per dataset. A schema per dataset would make the frozen openapi.json
change every time a dataset was added, which would turn the contract lock
into noise nobody reads.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from yfin.api.auth.dependencies import Authenticated
from yfin.api.core.errors import (
    TYPE_INVALID_CURSOR,
    TYPE_INVALID_PARAMETER,
    TYPE_NOT_FOUND,
    ApiProblem,
)
from yfin.api.ratelimit.dependencies import meter
from yfin.api.schemas.common import DEFAULT_PAGE_SIZE, Collection
from yfin.api.storage import catalog, limits
from yfin.api.storage import cursor as cursors
from yfin.api.storage.session import session_scope

router = APIRouter(prefix="/v1/datasets", tags=["datasets"])

SessionDep = Annotated[Session, Depends(session_scope)]

#: Query parameters that steer the request rather than filter the rows.
RESERVED = frozenset({"symbol", "limit", "cursor", "all"})


class CatalogEntryOut(BaseModel):
    name: str
    family: str
    scope: str
    kind: str
    table: str
    sort_key: list[str]
    descending: bool
    filters: list[str]
    symbol_scoped: bool
    description: str


@router.get("", response_model=Collection[CatalogEntryOut], summary="Dataset catalogue")
def list_datasets(
    response: Response,
    principal: Authenticated,
    all_datasets: Annotated[
        bool,
        Query(
            alias="all",
            description="List every dataset, not only those this token can read.",
        ),
    ] = False,
) -> Collection[CatalogEntryOut]:
    """What this token can read, by default.

    Filtering to the caller's scopes is the default because advertising
    data they cannot fetch is noise. `?all=true` is the documented way to
    see the rest -- a deliberate choice, not an accident of implementation.
    """
    entries = catalog.visible_to(principal.scopes, everything=all_datasets)
    response.headers["Cache-Control"] = "private, max-age=300"
    response.headers["Vary"] = "Authorization"
    return Collection[CatalogEntryOut](
        data=[CatalogEntryOut(**entry.describe()) for entry in entries]
    )


@router.get(
    "/{name}", response_model=Collection[dict[str, Any]], summary="Dataset rows"
)
def read_dataset(
    request: Request,
    response: Response,
    session: SessionDep,
    name: str,
    principal: Authenticated,
    symbol: str | None = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
    cursor: str | None = None,
) -> Collection[dict[str, Any]]:
    """Rows from one dataset.

    Any query parameter that is not reserved is treated as a filter, and
    only the columns the dataset declared as filterable are accepted --
    an unknown one is refused rather than ignored, because silently
    dropping a filter returns more data than the caller asked for and
    looks like it worked.
    """
    entry = catalog.CATALOG.get(name)

    # Scope before existence: 404-versus-403 is how an unauthorised caller
    # would enumerate dataset names.
    required = entry.scope if entry is not None else None
    if required is not None and required not in principal.scopes:
        raise ApiProblem(
            403,
            "insufficient_scope",
            "The token does not carry the required scope",
            detail=f"required scope: {required}",
            headers={'WWW-Authenticate': f'Bearer error="insufficient_scope", scope="{required}"'},
        )
    if entry is None:
        raise ApiProblem(404, TYPE_NOT_FOUND, "No such dataset")

    meter(request, response, principal, entry.family)
    limits.apply_statement_timeout(session)

    filters = _filters(request, entry)
    code = symbol.strip().upper() if symbol else None
    if entry.has_symbol and code is None:
        raise ApiProblem(
            422,
            TYPE_INVALID_PARAMETER,
            "This dataset is symbol-scoped",
            detail="pass ?symbol=... ; an unfiltered scan of a symbol-scoped "
            "table is never what a caller wants and never cheap",
        )

    size = _page_size(request, limit)
    identity = {
        "route": "dataset",
        "name": name,
        "symbol": code,
        "filters": dict(sorted(filters.items())),
        "limit": size,
    }
    after = _decode(cursor, query=identity, arity=len(entry.exposure.sort_key))

    rows, next_key = catalog.query(
        session, entry, symbol=code, filters=filters, limit=size, after=after
    )
    response.headers["Cache-Control"] = "private, max-age=60"
    response.headers["Vary"] = "Authorization, Accept-Encoding"
    return Collection[dict[str, Any]](
        data=[_serialise(row) for row in rows],
        next_cursor=cursors.encode(next_key, query=identity) if next_key else None,
    )


def _filters(request: Request, entry: catalog.CatalogEntry) -> dict[str, str]:
    allowed = set(entry.exposure.filters)
    supplied = {
        key: value
        for key, value in request.query_params.items()
        if key not in RESERVED
    }
    unknown = sorted(set(supplied) - allowed)
    if unknown:
        raise ApiProblem(
            422,
            TYPE_INVALID_PARAMETER,
            "Unknown filter",
            detail=(
                f"{', '.join(unknown)} cannot be filtered on this dataset; "
                f"allowed: {', '.join(sorted(allowed)) or 'none'}"
            ),
        )
    for value in supplied.values():
        if len(value) > limits.MAX_PARAM_LENGTH:
            raise ApiProblem(
                422, TYPE_INVALID_PARAMETER, "Filter value too long"
            )
    return supplied


def _page_size(request: Request, requested: int | None) -> int:
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


def _decode(
    cursor: str | None, *, query: dict[str, Any], arity: int
) -> tuple[Any, ...] | None:
    if cursor is None:
        return None
    try:
        return cursors.decode(cursor, query=query, arity=arity)
    except cursors.InvalidCursor as exc:
        raise ApiProblem(
            422, TYPE_INVALID_CURSOR, "The cursor is not usable here", detail=str(exc)
        ) from exc


def _serialise(row: dict[str, Any]) -> dict[str, Any]:
    """Decimals become strings here too.

    The generic surface has no hand-written schema to do it, so it happens
    on the way out. Emitting them as JSON numbers would undo the whole
    reason the columns are Numeric.
    """
    from decimal import Decimal

    return {
        key: (format(value, "f") if isinstance(value, Decimal) else value)
        for key, value in row.items()
    }
