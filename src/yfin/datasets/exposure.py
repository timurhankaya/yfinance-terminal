"""How a dataset opts in to the generic read surface.

Opt-in, failing closed: an undeclared dataset is absent from the catalogue and
404s. One declaration carries scope, billing family and sort key together.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from yfin.core.families import DataFamily, scope_for


@dataclass(frozen=True)
class ApiExposure:
    """One readable resource, declared by the dataset that writes it.

    A dataset may declare several: it is a write-side unit (one fetch,
    several tables) while the read side wants one resource per table.
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

    #: Whether the table may be browsed without a symbol filter. Only set
    #: it on a table with an index on its time column (calendars), or the
    #: browse becomes a sort over the whole table.
    symbol_optional: bool = False

    #: The name this resource is served under. Empty means "the dataset's
    #: own name", which is right when a dataset exposes exactly one thing.
    name: str = ""

    #: Columns this resource does NOT serve. A deny-list, so a column added
    #: later is served unless someone decides otherwise.
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

        Raises at registration so a misdeclared dataset stops the process
        from starting rather than surfacing as a 500.
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
