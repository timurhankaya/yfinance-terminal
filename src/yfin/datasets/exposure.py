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
    """A dataset's contract with the generic read surface."""

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

    #: One line for the catalogue.
    description: str = ""

    @property
    def scope(self) -> str:
        return scope_for(self.family)

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
        overlap = {name for name, _ in self.fixed} & set(self.filters)
        if overlap:
            raise ValueError(
                f"{dataset_name}: {sorted(overlap)} are both fixed and caller-settable; "
                "a caller could otherwise override the slice this dataset owns"
            )
