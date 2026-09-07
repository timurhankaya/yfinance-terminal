"""How a dataset opts in to the generic read surface.

Exposure is opt-in, and absence is the safe default: a dataset that
declares nothing here does not appear in the catalogue and returns 404
from `/v1/datasets/{name}`. Registering a new dataset therefore cannot
leave it readable under the wrong scope, or readable at all, by accident.

The alternative -- a mandatory `family` on every dataset -- was
considered and rejected. It buys no safety over failing closed; it only
forces every existing dataset to be classified before any of them can be
served, and it makes adding a dataset a two-part decision when the second
part is usually "not yet".

One declaration carries everything the API needs, so the scope required,
the usage family billed and the sort key paged on cannot disagree with
each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from yfin.core.families import DataFamily, scope_for


@dataclass(frozen=True)
class ApiExposure:
    """One readable resource, declared by the dataset that writes it.

    A dataset may declare several. That is not a convenience: a dataset is
    a WRITE-side unit -- one fetch, one or more tables -- while the read
    side wants resources. `funds_data` writes four tables and `search`
    eight; with one exposure per dataset their data was unreachable, and
    three published scopes granted access to nothing.
    """

    #: Decides the scope required and the usage counter billed.
    family: DataFamily

    #: Which of the dataset's `produces` tables is served. Datasets that
    #: write several tables have to say which one is the readable one;
    #: guessing "the first" would silently expose a side table.
    table: str

    #: Columns the keyset pages on, in order. Must be unique together, or
    #: pagination would skip or repeat rows -- the exact failure keyset
    #: pagination exists to avoid.
    sort_key: tuple[str, ...]

    #: Newest-first for time series, oldest-first for reference data.
    descending: bool = False

    #: Equality filters a caller may apply, beyond `symbol`.
    filters: tuple[str, ...] = field(default_factory=tuple)

    #: Filters applied unconditionally, as (column, value) pairs.
    #: Dataset and table are not one to one: `institutional_holders` and
    #: `mutualfund_holders` write the same table and are told apart by a
    #: column. Without this, asking for one would return the other's rows
    #: as well -- wrong, and wrong in a way a caller cannot see.
    fixed: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    #: Whether the table may be browsed without a symbol filter.
    #:
    #: The default is no, and that default earns its keep: an unfiltered
    #: scan of a symbol-keyed table is neither what a caller wants nor
    #: cheap to serve. Calendars are the exception -- "what reports this
    #: week" is the whole point of them -- and they can afford it because
    #: each carries an index on its time column, so the browse is an index
    #: scan rather than a sort over the table. Setting this without such
    #: an index would create exactly the endpoint §5.5 exists to prevent:
    #: cheap to send, expensive to serve.
    symbol_optional: bool = False

    #: The name this resource is served under. Empty means "the dataset's
    #: own name", which is right when a dataset exposes exactly one thing.
    name: str = ""

    #: Columns this resource does NOT serve.
    #:
    #: Without it a table is all or nothing, and one operational column is
    #: enough to make an otherwise useful resource unpublishable. `screens`
    #: is the case that forced it: a caller can filter three resources by
    #: `screen_key` but had no way to discover which keys exist, purely
    #: because the same row carries the screen's query definition.
    #:
    #: Hiding, not projecting: the default stays "everything the table
    #: has", so a column added later is served unless someone decides
    #: otherwise. An allow-list would silently drop new columns instead.
    hidden: tuple[str, ...] = field(default_factory=tuple)

    #: One line for the catalogue.
    description: str = ""

    @property
    def scope(self) -> str:
        return scope_for(self.family)

    def resource_name(self, dataset_name: str) -> str:
        return self.name or dataset_name

    def validate(self, *, dataset_name: str, produces: tuple[str, ...]) -> None:
        """Checks what can be checked without touching the database.

        Raises at registration -- that is, at import time -- rather than
        on the first request. A misdeclared dataset should stop the
        process from starting, not surface as a 500 to whoever happens to
        call it first.
        """
        if not isinstance(self.family, DataFamily):
            raise ValueError(f"{dataset_name}: api.family must be a DataFamily")
        if self.table not in produces:
            raise ValueError(
                f"{dataset_name}: api.table {self.table!r} is not among the tables "
                f"this dataset produces ({', '.join(produces) or 'none'})"
            )
        if not self.sort_key:
            raise ValueError(f"{dataset_name}: api.sort_key must not be empty")
        conflicting = set(self.hidden) & (
            set(self.sort_key) | set(self.filters) | {name for name, _ in self.fixed}
        )
        if conflicting:
            raise ValueError(
                f"{dataset_name}: {sorted(conflicting)} are hidden but also used to "
                "sort, filter or slice; a caller cannot page on a column they "
                "never see"
            )
        overlap = {name for name, _ in self.fixed} & set(self.filters)
        if overlap:
            raise ValueError(
                f"{dataset_name}: {sorted(overlap)} are both fixed and caller-settable; "
                "a caller could otherwise override the slice this dataset owns"
            )
