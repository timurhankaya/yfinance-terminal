"""Every produced table is routed, and routed the way the API serves it.

The routing map is written out by hand so the rule behind it -- family per
table, partition column per table -- is readable rather than inferred. That
only works if something checks the map against the schema it describes, and
these are those checks: a new table, a renamed column or a dataset exposed
under a second family fails here rather than sending a row to a topic no
consumer's ACL covers.
"""

from __future__ import annotations

import yfin.api.models  # noqa: F401  -- puts the API tables in the metadata
import yfin.models  # noqa: F401
from yfin.datasets import DOMAIN_DATASETS, MARKET_DATASETS, SYMBOL_DATASETS
from yfin.models.base import Base
from yfin.storage.routing import GATE_TABLES, INFRASTRUCTURE_TABLES, ROUTES

#: Produced and infrastructure at once: written by the bar datasets, but a
#: record of the pipeline's own repair work rather than data anyone mirrors.
_PRODUCED_INFRASTRUCTURE = frozenset({"bar_gaps"}) | GATE_TABLES


def _produced_tables() -> set[str]:
    names: set[str] = set()
    for registry in (SYMBOL_DATASETS, MARKET_DATASETS, DOMAIN_DATASETS):
        for name in registry:
            names.update(registry[name].produces)
    return names


def _exposures() -> list[tuple[str, str, str]]:
    """(dataset, table, family) for every declared exposure."""
    out: list[tuple[str, str, str]] = []
    for registry in (SYMBOL_DATASETS, MARKET_DATASETS, DOMAIN_DATASETS):
        for name in registry:
            dataset = registry[name]
            out.extend((dataset.name, e.table, e.family.value) for e in dataset.api)
    return out


def test_every_produced_table_is_routed_or_infrastructure() -> None:
    unrouted = _produced_tables() - set(ROUTES) - _PRODUCED_INFRASTRUCTURE
    assert not unrouted, f"produced but not routed: {sorted(unrouted)}"


def test_routes_cover_nothing_that_is_not_produced() -> None:
    extra = set(ROUTES) - _produced_tables()
    assert not extra, f"routed but produced by no dataset: {sorted(extra)}"


def test_the_route_family_is_the_family_the_api_serves_the_table_under() -> None:
    """One ACL line per family only holds if both sides agree.

    A table served as `fundamentals` and published as `bars` would need a
    consumer to hold two scopes to see one table -- which is exactly the
    promise the per-table map exists to keep.
    """
    for dataset, table, family in _exposures():
        assert table in ROUTES, f"{dataset}: exposed table {table!r} has no route"
        assert ROUTES[table].family.value == family, (
            f"{dataset}: {table} is served as {family} but routed as "
            f"{ROUTES[table].family.value}"
        )


def test_every_partition_column_exists_on_its_table() -> None:
    for table, route in ROUTES.items():
        columns = Base.metadata.tables[table].columns
        assert route.partition_column in columns, (
            f"{table}: partition column {route.partition_column!r} is not a column"
        )


def test_every_partition_column_is_part_of_the_primary_key() -> None:
    """A delete event has the key and nothing else.

    There is no row left to read a column from, so a partition column
    outside the primary key would leave deletes on that table unroutable --
    and the failure would only appear the first time something was deleted.
    """
    for table, route in ROUTES.items():
        key = [c.name for c in Base.metadata.tables[table].primary_key]
        assert route.partition_column in key, (
            f"{table}: partition column {route.partition_column!r} is not in the "
            f"primary key {key}, so a delete could not be routed"
        )


def test_infrastructure_covers_every_table_no_dataset_produces() -> None:
    """Nothing falls between the two lists.

    A table that is neither produced nor named as infrastructure is a table
    whose rows nobody decided about -- and the default has to be a decision,
    not silence.
    """
    unclassified = set(Base.metadata.tables) - _produced_tables() - INFRASTRUCTURE_TABLES
    assert not unclassified, f"neither produced nor infrastructure: {sorted(unclassified)}"


def test_infrastructure_tables_all_exist() -> None:
    missing = INFRASTRUCTURE_TABLES - set(Base.metadata.tables)
    assert not missing, f"infrastructure tables not in the schema: {sorted(missing)}"


def test_gate_tables_are_infrastructure() -> None:
    assert GATE_TABLES <= INFRASTRUCTURE_TABLES


def test_no_table_is_both_routed_and_infrastructure() -> None:
    both = set(ROUTES) & INFRASTRUCTURE_TABLES
    assert not both, f"routed and infrastructure at once: {sorted(both)}"
