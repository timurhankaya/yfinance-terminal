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
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from yfin.api.auth.dependencies import Authenticated, insufficient_scope
from yfin.api.core.errors import (
    TYPE_INVALID_PARAMETER,
    TYPE_NOT_FOUND,
    ApiProblem,
)
from yfin.api.ratelimit.dependencies import attribute_family, meter
from yfin.api.routers.v1 import paging
from yfin.api.schemas.common import Collection
from yfin.api.storage import catalog, limits
from yfin.api.storage import cursor as cursors
from yfin.api.storage.session import session_scope
from yfin.core.families import META_FAMILY

router = APIRouter(prefix="/v1/datasets", tags=["datasets"])

SessionDep = Annotated[Session, Depends(session_scope)]

#: Query parameters that steer the request rather than filter the rows.
RESERVED = frozenset({"symbol", "limit", "cursor", "all"})


class ColumnOut(BaseModel):
    """One column of a dataset, as it arrives.

    This is what stands in for a schema per resource. The data route
    serves `dict[str, Any]` and will keep doing so -- a schema per dataset
    would make the frozen openapi.json churn every time one was added --
    so the shape has to be discoverable somewhere, and the catalogue is
    where a client is already looking.
    """

    name: str
    type: str = Field(
        description=(
            "The wire type, not the SQL type: `string (decimal)` for exact "
            "numbers, `string (date-time)`, `string (date)`, `string`, "
            "`integer` or `boolean`."
        )
    )
    nullable: bool


class CatalogEntryOut(BaseModel):
    """The catalogue as clients see it.

    The mapping lives here rather than on the catalogue entry: the wire
    shape is an HTTP concern, and `api/storage` is not allowed to know
    one. A `describe()` on the entry would have made the storage layer
    the place where a field rename in the contract has to be made.
    """

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
    columns: list[ColumnOut] = Field(
        description="Every column the rows carry, in table order."
    )

    @classmethod
    def of(cls, entry: catalog.CatalogEntry) -> CatalogEntryOut:
        return cls(
            name=entry.name,
            family=entry.family.value,
            scope=entry.scope,
            kind=entry.kind,
            table=entry.table.name,
            sort_key=list(entry.exposure.sort_key),
            descending=entry.exposure.descending,
            filters=list(entry.exposure.filters),
            symbol_scoped=entry.has_symbol,
            description=entry.exposure.description,
            # The same property the query selects from, so the document
            # cannot describe a row shape the route does not send.
            columns=[
                ColumnOut(
                    name=column.name,
                    type=catalog.wire_type(column),
                    nullable=bool(column.nullable),
                )
                for column in entry.served_columns
            ],
        )


@router.get("", response_model=Collection[CatalogEntryOut], summary="Dataset catalogue")
def list_datasets(
    request: Request,
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
    # Metered like everything else. It was not, and a token holder could
    # therefore drive this route at any rate they liked -- it still costs a
    # signature check, a Redis read and a worker thread each time. `meta`
    # is the family reserved for exactly this: a surface that belongs to
    # no data family.
    meter(request, response, principal, META_FAMILY)
    entries = catalog.visible_to(principal.scopes, everything=all_datasets)
    response.headers["Cache-Control"] = "private, max-age=300"
    response.headers["Vary"] = "Authorization"
    return Collection[CatalogEntryOut](
        data=[CatalogEntryOut.of(entry) for entry in entries]
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
    symbol: Annotated[
        str | None,
        Query(
            description="Required for a symbol-scoped dataset, optional for the "
            "few that may be browsed. The catalogue says which is which."
        ),
    ] = None,
    limit: Annotated[
        int | None,
        Query(ge=1, description="Rows per page. Capped by the plan; over it is a 422."),
    ] = None,
    cursor: Annotated[
        str | None,
        Query(description="The `next_cursor` of the previous page of THIS query."),
    ] = None,
) -> Collection[dict[str, Any]]:
    """Rows from one dataset.

    Any query parameter that is not reserved is treated as a filter, and
    only the columns the dataset declared as filterable are accepted --
    an unknown one is refused rather than ignored, because silently
    dropping a filter returns more data than the caller asked for and
    looks like it worked.
    """
    # Metered BEFORE the catalogue is consulted, and deliberately so. The
    # refusals below are the cheapest thing a caller can ask for and were
    # the only unmetered path in the API: unlimited 403s and 404s, none of
    # them counted, each one a signature check and a Redis read. The real
    # family is attributed once the name resolves.
    meter(request, response, principal, META_FAMILY)
    entry = catalog.CATALOG.get(name)

    # Scope before existence: 404-versus-403 is how an unauthorised caller
    # would enumerate dataset names.
    required = entry.scope if entry is not None else None
    if required is not None and required not in principal.scopes:
        # The same factory the scoped routes raise, not a second copy: the
        # type was a bare string here and the challenge header a verbatim
        # duplicate, so renaming the constant would have left two
        # different 403 bodies in one API.
        raise insufficient_scope(required)
    if entry is None:
        raise ApiProblem(404, TYPE_NOT_FOUND, "No such dataset")

    attribute_family(request, entry.family)
    limits.apply_statement_timeout(session)

    filters = _filters(request, entry)
    code = symbol.strip().upper() if symbol else None
    if entry.has_symbol and code is None and not entry.exposure.symbol_optional:
        raise ApiProblem(
            422,
            TYPE_INVALID_PARAMETER,
            "This dataset is symbol-scoped",
            detail="pass ?symbol=... ; an unfiltered scan of a symbol-scoped "
            "table is never what a caller wants and never cheap",
        )

    size = paging.page_size(request, limit)
    identity = {
        "route": "dataset",
        "name": name,
        "symbol": code,
        "filters": dict(sorted(filters.items())),
        "limit": size,
    }
    after = paging.decode_cursor(cursor, query=identity, arity=len(entry.exposure.sort_key))

    rows, next_key = catalog.query(
        session, entry, symbol=code, filters=filters, limit=size, after=after
    )
    response.headers["Cache-Control"] = "private, max-age=60"
    response.headers["Vary"] = "Authorization, Accept-Encoding"
    return Collection[dict[str, Any]](
        data=[paging.serialise_row(row) for row in rows],
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


