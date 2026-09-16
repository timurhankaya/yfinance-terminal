"""Every dataset declares what its write policy needs. The policies annotate attributes
without values, so a dataset that forgets one raises `AttributeError` at the first write,
after the Yahoo call has been paid for."""

from __future__ import annotations

import pytest

import yfin.datasets  # noqa: F401  -- registration happens on import
from yfin.datasets.registry import DOMAIN_DATASETS, MARKET_DATASETS, SYMBOL_DATASETS

#: Every registered dataset, whatever axis it runs on.
ALL = [
    registry[name]
    for registry in (SYMBOL_DATASETS, MARKET_DATASETS, DOMAIN_DATASETS)
    for name in registry
]


def _required(dataset: object) -> set[str]:
    """Attributes the dataset's bases annotate but never assign, read from the hierarchy so
    a policy that grows a fourth attribute is covered here too."""
    needed: set[str] = set()
    for klass in type(dataset).__mro__:
        for name, _ in getattr(klass, "__annotations__", {}).items():
            if name.startswith("_"):
                continue
            if name not in vars(klass):
                needed.add(name)
    return needed


def test_there_are_datasets_to_check() -> None:
    """Guards the guard: an import that stopped registering would
    otherwise make every assertion below vacuous."""
    assert len(ALL) > 50


@pytest.mark.parametrize("dataset", ALL, ids=lambda ds: str(ds.name))
def test_every_declared_write_attribute_is_set(dataset: object) -> None:
    missing = sorted(name for name in _required(dataset) if not hasattr(dataset, name))
    assert missing == [], f"{dataset.name} never sets {missing}"


@pytest.mark.parametrize("dataset", ALL, ids=lambda ds: str(ds.name))
def test_a_snapshot_dataset_names_its_key(dataset: object) -> None:
    """The one that actually bit: `key_columns` is ("symbol",) for three
    of the five snapshot datasets and wrong for the other two, so it has
    no default and each one says which columns its snapshot is keyed by."""
    from yfin.datasets.snapshot_base import SnapshotWrite

    if not isinstance(dataset, SnapshotWrite):
        return
    key = dataset.key_columns
    assert isinstance(key, tuple) and key, f"{dataset.name} has an empty key"
    assert all(isinstance(column, str) for column in key)
