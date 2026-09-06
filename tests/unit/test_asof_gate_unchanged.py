"""Proof that extracting `AsOfGate` changed zero behavior.

The mixin was split out of the `Dataset` hierarchy and `first_seen_at` was
added to `VOLATILE_COLUMNS`. This file proves two things:

1. The existing 13 as-of datasets' gate table, key columns, and gate
   identity are exactly what they were before.
2. Adding `first_seen_at` cannot change any existing hash -- because no
   data table on the symbol side carries that column. The proof is derived
   from the schema, not from a hand-written list.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from yfin.datasets import SYMBOL_DATASETS
from yfin.datasets.asof_base import (
    GATE_KEY_COLUMNS,
    GATE_TABLE,
    VOLATILE_COLUMNS,
    AsOfDataset,
    AsOfGate,
)
from yfin.datasets.base import Dataset, NormalizedResult, TableWrite
from yfin.models import Base

AS_OF = date(2026, 9, 4)
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)

# GOLDEN VALUE: SHA-256 of the deterministic body below. Not tied to a
# fixture (so it survives fixtures being recaptured); what it fences is the
# `content_hash` ALGORITHM -- column exclusion, table ordering, row ordering.
GOLDEN_HASH = "2a49b87018fd6c89edce127efc34f7c15df1baf6241c27eb479fd09b618076fe"


def _golden_result(order: tuple[str, ...]) -> NormalizedResult:
    return NormalizedResult(
        writes=[
            TableWrite(
                table="institutional_holders",
                rows=[
                    {
                        "symbol": "AAPL",
                        "as_of_date": AS_OF,
                        "holder_type": "institution",
                        "holder": holder,
                        "shares": Decimal("10"),
                        "fetched_at": NOW,
                    }
                    for holder in order
                ],
                key_columns=("symbol", "as_of_date", "holder_type", "holder"),
                update_columns=("shares", "fetched_at"),
            )
        ]
    )


def _asof_datasets() -> list[AsOfDataset[Any]]:
    """The `asof_state` gate family -- the 13 datasets this file is about.

    Filtered by gate table, not just by type: `search`/`lookup` are also
    `AsOfDataset`s but use their own gate (`discovery_asof_state`).
    Filtering by type alone would break this file's "the 13 datasets'
    contract is unchanged" claim every time an unrelated family grows.
    """
    return [
        SYMBOL_DATASETS[name]
        for name in SYMBOL_DATASETS
        if isinstance(SYMBOL_DATASETS[name], AsOfDataset)
        and SYMBOL_DATASETS[name].asof_gate_table == GATE_TABLE
    ]


def _discovery_datasets() -> list[AsOfDataset[Any]]:
    """The `discovery_asof_state` gate family."""
    from yfin.datasets.discovery.base import DISCOVERY_GATE_TABLE

    return [
        SYMBOL_DATASETS[name]
        for name in SYMBOL_DATASETS
        if isinstance(SYMBOL_DATASETS[name], AsOfDataset)
        and SYMBOL_DATASETS[name].asof_gate_table == DISCOVERY_GATE_TABLE
    ]


def test_there_are_still_thirteen_symbol_side_asof_datasets() -> None:
    assert len(_asof_datasets()) == 13


def test_symbol_side_gate_contract_is_unchanged() -> None:
    for dataset in _asof_datasets():
        assert dataset.asof_gate_table == GATE_TABLE == "asof_state"
        assert dataset.asof_gate_key_columns == GATE_KEY_COLUMNS == ("symbol", "dataset")
        assert dataset.produces[-1] == GATE_TABLE


def test_symbol_side_gate_identity_is_symbol_and_dataset() -> None:
    dataset = SYMBOL_DATASETS["institutional_holders"]
    result = NormalizedResult(
        writes=[
            TableWrite(
                table="institutional_holders",
                rows=[
                    {
                        "symbol": "AAPL",
                        "as_of_date": AS_OF,
                        "holder": "Vanguard",
                        "shares": Decimal("10"),
                        "fetched_at": NOW,
                    }
                ],
                key_columns=("symbol", "as_of_date", "holder_type", "holder"),
                update_columns=("shares",),
            )
        ]
    )
    assert dataset.gate_identity(result) == {
        "symbol": "AAPL",
        "dataset": "institutional_holders",
    }


def test_first_seen_at_cannot_affect_any_existing_hash() -> None:
    """No data table in the `asof_state` family carries `first_seen_at`.

    The only symbol-side table carrying that column is the gate itself
    (`asof_state`), and that never enters the hash body.
    """
    carriers = {
        table.name
        for table in Base.metadata.tables.values()
        if "first_seen_at" in table.c
    }
    symbol_side_targets = {
        table
        for dataset in _asof_datasets()
        for table in dataset.produces
        if table != GATE_TABLE
    }
    assert symbol_side_targets & carriers == set()
    assert "first_seen_at" in VOLATILE_COLUMNS
    assert GATE_TABLE in carriers


def test_discovery_family_uses_its_own_gate() -> None:
    """`asof_state` cannot be used here.

    That table's `symbol` column carries an FK to `symbols.symbol`; a free
    search term is not in there, so a gate row would raise `ERROR 1452`.
    This test stops someone moving discovery datasets to the old gate "for
    consistency".
    """
    from yfin.datasets.discovery.base import (
        DISCOVERY_GATE_KEY_COLUMNS,
        DISCOVERY_GATE_TABLE,
    )

    family = _discovery_datasets()
    assert {d.name for d in family} == {"search", "lookup"}
    for dataset in family:
        assert dataset.asof_gate_table == DISCOVERY_GATE_TABLE
        assert dataset.asof_gate_key_columns == DISCOVERY_GATE_KEY_COLUMNS
        assert dataset.produces[-1] == DISCOVERY_GATE_TABLE


def test_discovery_first_seen_at_carrier_is_ungated() -> None:
    """`research_reports` carries `first_seen_at` and is in `search`'s
    `produces`, but sits outside the gate.

    So it never enters the hash body, and `first_seen_at`'s "changes every
    run" nature cannot break the gate. This is the discovery-family
    counterpart of the test above; the proof is derived from
    `UNGATED_TABLES`, not a hand-written exception list.
    """
    from yfin.datasets.discovery.base import UNGATED_TABLES

    carriers = {
        table.name for table in Base.metadata.tables.values() if "first_seen_at" in table.c
    }
    gated_targets = {
        table
        for dataset in _discovery_datasets()
        for table in dataset.produces
        if table != dataset.asof_gate_table and table not in UNGATED_TABLES
    }
    assert gated_targets & carriers == set()
    # Proof the claim is not vacuous: the carrier is really in `produces`
    # and really outside the gate.
    assert "research_reports" in carriers
    assert "research_reports" in SYMBOL_DATASETS["search"].produces
    assert "research_reports" in UNGATED_TABLES


def test_asof_dataset_is_still_a_dataset() -> None:
    """LSP: the symbol-side runner expects a `Dataset`; the mixin does not break that."""
    for dataset in _asof_datasets():
        assert isinstance(dataset, Dataset)
        assert isinstance(dataset, AsOfGate)


def test_domain_side_uses_a_different_gate_without_touching_the_symbol_side() -> None:
    from yfin.datasets.domain.base import DomainAsOfDataset
    from yfin.datasets.registry import DOMAIN_DATASETS

    for name in DOMAIN_DATASETS:
        dataset = DOMAIN_DATASETS[name]
        if not isinstance(dataset, DomainAsOfDataset):
            continue
        assert dataset.asof_gate_table == "domain_asof_state"
        # The two hierarchies cannot merge: a domain dataset is not a
        # `Dataset` and must not be (fetch/normalize signatures differ).
        assert not isinstance(dataset, Dataset)
        assert isinstance(dataset, AsOfGate)


def test_content_hash_matches_the_golden_value() -> None:
    """Regression guard for the hash algorithm.

    `AsOfGate`'s `content_hash` body was moved during the refactor but not
    changed; this value pins it permanently.
    """
    dataset = SYMBOL_DATASETS["institutional_holders"]
    assert dataset.content_hash(_golden_result(("Vanguard", "BlackRock"))) == GOLDEN_HASH


def test_row_order_does_not_change_the_golden_value() -> None:
    dataset = SYMBOL_DATASETS["institutional_holders"]
    assert dataset.content_hash(_golden_result(("BlackRock", "Vanguard"))) == GOLDEN_HASH
