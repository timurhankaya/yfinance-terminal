"""The generic dataset surface: one pattern over every dataset that opts
in. The catalogue is built at import and validated against the real
schema then, so a bad column is a startup failure rather than a 500. The
scope comes from the dataset's family, so listing and authorisation read
one declaration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Column, Select, Table, and_, or_, select
from sqlalchemy.orm import Session

from yfin.core.families import DataFamily, scope_for
from yfin.datasets import DOMAIN_DATASETS, MARKET_DATASETS, SYMBOL_DATASETS
from yfin.datasets.axis import DatasetAxis
from yfin.datasets.exposure import ApiExposure
from yfin.models.base import Base
from yfin.storage.wire import wire_type


@dataclass(frozen=True)
class CatalogEntry:
    #: The resource name, which is the dataset's name unless the dataset
    #: exposes several things and had to name them apart.
    name: str
    #: The dataset that writes it. Kept so a clash names both sides.
    dataset: str
    family: DataFamily
    #: Which registry the dataset was registered in. Not re-declared as
    #: bare strings here: `Turn.kind` on the pipeline side is the same
    #: three-valued concept, and this one is published on the wire.
    kind: DatasetAxis
    table: Table
    exposure: ApiExposure

    @property
    def scope(self) -> str:
        return scope_for(self.family)

    @property
    def served_columns(self) -> tuple[Column[Any], ...]:
        """The columns a caller receives, in table order. Used by both the
        query and the catalogue's column list so they cannot disagree."""
        hidden = set(self.exposure.hidden)
        return tuple(c for c in self.table.columns if c.name not in hidden)

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
        (DatasetAxis.SYMBOL, SYMBOL_DATASETS),
        (DatasetAxis.MARKET, MARKET_DATASETS),
        (DatasetAxis.DOMAIN, DOMAIN_DATASETS),
    ):
        # The registry is a collection of names; iterating it and
        # indexing is its published shape, not a second accessor.
        for name in registry:
            dataset = registry[name]
            # `dataset.api`, not `getattr(...)`: the bases declare the field,
            # and a reflective read would hide a misspelled declaration.
            for exposure in dataset.api:
                _add(entries, kind, dataset.name, exposure)
    return entries


def _add(
    entries: dict[str, CatalogEntry],
    kind: DatasetAxis,
    dataset_name: str,
    exposure: ApiExposure,
) -> None:
    """Registers one readable resource, checked against the real schema at
    import so a bad column stops the process from starting."""
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
            *exposure.hidden,
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


#: Built at import so a bad declaration stops the process from starting.
CATALOG: dict[str, CatalogEntry] = _build()

# Checked here rather than lazily, for the same reason `_add` validates
# columns here: a dataset exposing a type the API cannot describe is a
# startup failure, not a surprise for whoever calls it first.
for _entry in CATALOG.values():
    for _column in _entry.served_columns:
        wire_type(_column)


def visible_to(scopes: frozenset[str], *, everything: bool) -> list[CatalogEntry]:
    """The catalogue as one caller sees it: filtered to the caller's scopes
    unless `everything`, so the default does not advertise data the caller
    cannot fetch."""
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
    statement: Select[Any] = select(*entry.served_columns)
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
    """Row-wise comparison, spelled out: PostgreSQL's `(a, b) > (:a, :b)`
    only covers ascending order, and a wrong direction silently returns the
    wrong page rather than an error."""
    clauses = []
    for index, column in enumerate(columns):
        equals = [columns[i] == after[i] for i in range(index)]
        compare = column < after[index] if descending else column > after[index]
        clauses.append(and_(*equals, compare) if equals else compare)
    return or_(*clauses)
