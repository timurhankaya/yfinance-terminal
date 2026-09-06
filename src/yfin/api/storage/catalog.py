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
    name: str
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
    def has_symbol(self) -> bool:
        return "symbol" in self.table.c

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "family": self.family.value,
            "scope": self.scope,
            "kind": self.kind,
            "table": self.table.name,
            "sort_key": list(self.exposure.sort_key),
            "descending": self.exposure.descending,
            "filters": list(self.exposure.filters),
            "symbol_scoped": self.has_symbol,
            "description": self.exposure.description,
        }


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
            exposure: ApiExposure | None = getattr(dataset, "api", None)
            if exposure is None:
                continue

            table = Base.metadata.tables.get(exposure.table)
            if table is None:
                raise ValueError(
                    f"{dataset.name}: api.table {exposure.table!r} is not a known table"
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
                    f"{dataset.name}: api declares columns {unknown} that "
                    f"{table.name} does not have"
                )
            entries[dataset.name] = CatalogEntry(
                name=dataset.name,
                family=exposure.family,
                kind=kind,
                table=table,
                exposure=exposure,
            )
    return entries


#: Built at import so a bad declaration stops the process from starting.
CATALOG: dict[str, CatalogEntry] = _build()


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
