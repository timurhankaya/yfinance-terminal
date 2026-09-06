"""date_range contract and SyncContext range fields."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from yfin.datasets.base import Dataset, NormalizedResult, SyncContext
from yfin.datasets.registry import SYMBOL_DATASETS

FETCHED_AT = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class _Probe(Dataset[None]):
    name = "_probe"

    def fetch(self, ctx: SyncContext) -> None:
        return None

    def normalize(self, raw: None, symbol: str) -> NormalizedResult:
        return NormalizedResult()


def _ctx(**kwargs: Any) -> SyncContext:
    return SyncContext("AAPL", object(), FETCHED_AT, **kwargs)


def test_date_range_defaults_to_none() -> None:
    """Default 'none': a dataset that declares no range does not run with --start."""
    assert _Probe.date_range == "none"


def test_sync_context_start_end_default_to_none() -> None:
    """An unfiltered run writes the ENTIRE history Yahoo returns."""
    ctx = _ctx()
    assert ctx.start is None
    assert ctx.end is None


def test_sync_context_carries_range() -> None:
    ctx = _ctx(start=date(2020, 1, 1), end=date(2021, 12, 31))
    assert ctx.start == date(2020, 1, 1)
    assert ctx.end == date(2021, 12, 31)


def test_every_registered_dataset_declares_valid_date_range() -> None:
    """date_range is a class attribute and must be one of three values."""
    for name in SYMBOL_DATASETS:
        value = SYMBOL_DATASETS[name].date_range
        assert value in ("api", "filter", "none"), f"{name} -> {value}"


def test_date_range_is_class_attribute_not_property() -> None:
    """If a subtype turns it into a property, the base contract narrows (LSP)."""
    for name in SYMBOL_DATASETS:
        cls = type(SYMBOL_DATASETS[name])
        attr = getattr(cls, "date_range", None)
        assert not isinstance(attr, property), f"{name} date_range property olmamali"
