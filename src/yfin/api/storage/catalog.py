"""The generic dataset surface.

Five hand-written endpoints cover the resources people ask for by name.
This covers the rest: one pattern over every dataset that opts in, so
adding a dataset to the API is a declaration rather than a new module.

Two things are resolved here and nowhere else.

The catalogue is built once, at import, and validated against the real
schema then. A dataset naming a column that does not exist is a startup
failure, not a 500 on the first request that touches it.

The scope comes from the dataset's family, so the catalogue and the
authorisation check read the same declaration. A dataset cannot be listed
as belonging to one family and served under another.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Column, Select, Table, and_, or_, select
from sqlalchemy import types as sqltypes
from sqlalchemy.orm import Session

from yfin.core.families import DataFamily, scope_for
from yfin.datasets import DOMAIN_DATASETS, MARKET_DATASETS, SYMBOL_DATASETS
from yfin.datasets.exposure import ApiExposure
from yfin.models.base import Base

#: Which registry a dataset was registered in. Not a property of the
#: dataset itself -- it is exactly the registry it lives in.
SCOPE_SYMBOL = "symbol"
SCOPE_MARKET = "market"
SCOPE_DOMAIN = "domain"


@dataclass(frozen=True)
class CatalogEntry:
    #: The resource name, which is the dataset's name unless the dataset
    #: exposes several things and had to name them apart.
    name: str
    #: The dataset that writes it. Kept so a clash names both sides.
    dataset: str
    family: DataFamily
    kind: str
    table: Table
    exposure: ApiExposure

    @property
    def scope(self) -> str:
        return scope_for(self.family)

    @property
    def sort_columns(self) -> tuple[Column[Any], ...]:
        return tuple(self.table.c[name] for name in self.exposure.sort_key)

    @property
    def symbol_required(self) -> bool:
        return self.has_symbol and not self.exposure.symbol_optional

    @property
    def has_symbol(self) -> bool:
        return "symbol" in self.table.c


def _build() -> dict[str, CatalogEntry]:
    entries: dict[str, CatalogEntry] = {}
    for kind, registry in (
        (SCOPE_SYMBOL, SYMBOL_DATASETS),
        (SCOPE_MARKET, MARKET_DATASETS),
        (SCOPE_DOMAIN, DOMAIN_DATASETS),
    ):
        # The registry is a collection of names; iterating it and
        # indexing is its published shape, not a second accessor.
        for name in registry:
            dataset = registry[name]
            for exposure in getattr(dataset, "api", ()):
                _add(entries, kind, dataset.name, exposure)
    return entries


def _add(
    entries: dict[str, CatalogEntry], kind: str, dataset_name: str, exposure: ApiExposure
) -> None:
    """Registers one readable resource, checked against the real schema.

    Validation happens here, at import, so a dataset naming a column its
    table does not have stops the process from starting rather than
    surfacing as a 500 to whoever calls it first.
    """
    resource = exposure.resource_name(dataset_name)
    if resource in entries:
        raise ValueError(
            f"{dataset_name}: resource {resource!r} is already registered by "
            f"{entries[resource].dataset}; give one of them an explicit api name"
        )

    table = Base.metadata.tables.get(exposure.table)
    if table is None:
        raise ValueError(
            f"{dataset_name}: api.table {exposure.table!r} is not a known table"
        )
    unknown = [
        column
        for column in (
            *exposure.sort_key,
            *exposure.filters,
            *(name for name, _ in exposure.fixed),
        )
        if column not in table.c
    ]
    if unknown:
        raise ValueError(
            f"{dataset_name}: api declares columns {unknown} that "
            f"{table.name} does not have"
        )

    entries[resource] = CatalogEntry(
        name=resource,
        dataset=dataset_name,
        family=exposure.family,
        kind=kind,
        table=table,
        exposure=exposure,
    )


#: How a stored column arrives on the wire.
#:
#: The wire type, not the SQL type: a caller does not care that a price is
#: Numeric(28,12), only that it arrives as a string it must not parse as a
#: float. Order matters -- Enum and VARCHAR are subclasses of String,
#: TIMESTAMP of DateTime -- so the checks run most specific first.
def wire_type(column: Column[Any]) -> str:
    """The JSON shape of one column, or a refusal.

    An unknown type raises, and it raises at import like every other bad
    declaration in this module. The alternative -- emitting "unknown", or
    500ing on the first request to /v1/datasets -- would publish a column
    shape nobody had checked. Today the exposed tables use eleven types
    and none of them is JSONB, ARRAY or bytea; the first one that is
    should stop the process, not reach a client.
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
        "wire type; teach api/storage/catalog.wire_type about it before exposing it"
    )


#: Built at import so a bad declaration stops the process from starting.
CATALOG: dict[str, CatalogEntry] = _build()

# Checked here rather than lazily, for the same reason `_add` validates
# columns here: a dataset exposing a type the API cannot describe is a
# startup failure, not a surprise for whoever calls it first.
for _entry in CATALOG.values():
    for _column in _entry.table.columns:
        wire_type(_column)


def visible_to(scopes: frozenset[str], *, everything: bool) -> list[CatalogEntry]:
    """The catalogue as one caller sees it.

    Filtered to the caller's scopes by default. `everything` is the
    documented opt-out, so "what else do you have" is answerable without
    guessing at names -- but it is a choice, not the default, because the
    default should not advertise data the caller cannot fetch.
    """
    entries = sorted(CATALOG.values(), key=lambda entry: entry.name)
    if everything:
        return entries
    return [entry for entry in entries if entry.scope in scopes]


def query(
    session: Session,
    entry: CatalogEntry,
    *,
    symbol: str | None,
    filters: dict[str, str],
    limit: int,
    after: tuple[Any, ...] | None,
) -> tuple[list[dict[str, Any]], tuple[Any, ...] | None]:
    statement: Select[Any] = select(entry.table)
    for name, value in entry.exposure.fixed:
        # The dataset's own slice of a shared table. Applied before
        # anything the caller sent, and not overridable by them.
        statement = statement.where(entry.table.c[name] == value)
    if symbol is not None and entry.has_symbol:
        statement = statement.where(entry.table.c.symbol == symbol)
    for name, value in filters.items():
        statement = statement.where(entry.table.c[name] == value)

    columns = entry.sort_columns
    if after is not None:
        statement = statement.where(_keyset(columns, after, entry.exposure.descending))

    order = [
        column.desc() if entry.exposure.descending else column.asc() for column in columns
    ]
    rows = session.execute(statement.order_by(*order).limit(limit + 1)).all()

    has_more = len(rows) > limit
    visible = rows[:limit]
    next_key = (
        tuple(getattr(visible[-1], name) for name in entry.exposure.sort_key)
        if has_more and visible
        else None
    )
    return [dict(row._mapping) for row in visible], next_key


def _keyset(
    columns: tuple[Column[Any], ...], after: tuple[Any, ...], descending: bool
) -> Any:
    """Row-wise comparison, spelled out.

    PostgreSQL supports `(a, b) > (:a, :b)` directly, but only for plain
    ascending order; a descending key needs the comparison inverted, and
    mixing the two silently returns the wrong page rather than an error.
    Writing it out keeps both directions in one place.
    """
    clauses = []
    for index, column in enumerate(columns):
        equals = [columns[i] == after[i] for i in range(index)]
        compare = column < after[index] if descending else column > after[index]
        clauses.append(and_(*equals, compare) if equals else compare)
    return or_(*clauses)
