"""Audit-record gaps and range-based skipping.

Every test in this file closes off the risk of a row NOT being written to
`sync_run_items`: an unwritten row means silent data loss.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from yfin.datasets.base import Dataset, NormalizedResult, SyncContext
from yfin.datasets.registry import Registry
from yfin.models import ItemStatus
from yfin.pipeline.runner import SymbolPayload, _failed_records, _record_items, _worker
from yfin.storage.contracts import TableWrite, WriteStats

FETCHED_AT = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class _Watchonly(Dataset[None]):
    """A watch dataset like `sustainability`: writes to no table."""

    name = "_watchonly"
    produces = ()

    def fetch(self, ctx: SyncContext) -> None:
        return None

    def normalize(self, raw: None, symbol: str) -> NormalizedResult:
        return NormalizedResult()


class _Boom(_Watchonly):
    name = "_boom"

    def fetch(self, ctx: SyncContext) -> None:
        raise RuntimeError("ag patladi")


class _Bootstrap(Dataset[None]):
    """Resolves the symbol. If it returns empty, the worker skips the
    symbol entirely (unknown_symbol), so it must return a populated
    TableWrite."""

    name = "symbols"
    produces = ("symbols",)

    def fetch(self, ctx: SyncContext) -> None:
        return None

    def normalize(self, raw: None, symbol: str) -> NormalizedResult:
        return NormalizedResult(
            writes=[
                TableWrite(
                    table="symbols",
                    rows=[{"symbol": symbol}],
                    key_columns=("symbol",),
                    update_columns=(),
                )
            ]
        )


class _Ranged(_Watchonly):
    name = "_ranged"
    date_range = "filter"


def test_watchonly_dataset_still_records_an_item() -> None:
    """A dataset with produces=() does not vanish from the audit."""
    records = _record_items(_Watchonly(), "AAPL", WriteStats(), fetched=0, duration_ms=1)
    assert len(records) == 1
    assert records[0].table_name is None
    assert records[0].status is ItemStatus.EMPTY


def test_watchonly_failure_still_records_an_item() -> None:
    """The same gap existed on the failure path too."""
    registry: Registry[Dataset[Any]] = Registry()
    registry.register(_Watchonly())
    records = _failed_records("AAPL", "_watchonly", "boom", registry=registry)
    assert len(records) == 1
    assert records[0].table_name is None
    assert records[0].status is ItemStatus.FAILED


def test_unknown_dataset_name_still_records_an_item() -> None:
    """A single NULL row is written even for a name absent from the registry."""
    registry: Registry[Dataset[Any]] = Registry()
    records = _failed_records("AAPL", "_yok", "boom", registry=registry)
    assert len(records) == 1
    assert records[0].table_name is None


def _run_worker(datasets: list[Dataset[Any]], **ctx_kwargs: Any) -> SymbolPayload:
    return _worker(
        "AAPL",
        datasets,
        _Bootstrap(),
        FETCHED_AT,
        watermarks=None,
        full_refresh=False,
        **ctx_kwargs,
    )


def test_range_skips_date_range_none_datasets() -> None:
    """When --start is given, a 'none' dataset is not run and gets recorded."""
    payload = _run_worker([_Watchonly(), _Ranged()], start=date(2020, 1, 1))
    assert [name for name, _ in payload.skipped] == ["_watchonly"]
    assert payload.skipped[0][1] == "date_range=none"
    ran = [ds.name for ds, *_ in payload.results]
    # Bootstrap is never skipped: no dataset can run before the symbol is
    # resolved, even with date_range="none"; skipping applies only to
    # datasets in the loop.
    assert ran == ["symbols", "_ranged"]


def test_no_range_runs_everything() -> None:
    """When no range is given, no dataset is skipped."""
    payload = _run_worker([_Watchonly(), _Ranged()])
    assert payload.skipped == []
    assert {ds.name for ds, *_ in payload.results} == {"symbols", "_watchonly", "_ranged"}


def test_skipped_datasets_become_item_records() -> None:
    """A skipped dataset does not vanish silently; it is written as SKIPPED."""
    from yfin.pipeline.runner import _skipped_records

    registry: Registry[Dataset[Any]] = Registry()
    registry.register(_Watchonly())
    records = _skipped_records("AAPL", "_watchonly", "date_range=none", registry=registry)
    assert len(records) == 1
    assert records[0].status is ItemStatus.SKIPPED
    assert records[0].error == "date_range=none"
    assert records[0].table_name is None


def test_worker_range_filter_reaches_context() -> None:
    """A 'filter' dataset sees the range via ctx."""
    seen: dict[str, Any] = {}

    class _Peek(_Ranged):
        name = "_peek"

        def fetch(self, ctx: SyncContext) -> None:
            seen["start"] = ctx.start
            seen["end"] = ctx.end
            return None

    _run_worker([_Peek()], start=date(2020, 1, 1), end=date(2021, 12, 31))
    assert seen == {"start": date(2020, 1, 1), "end": date(2021, 12, 31)}


@pytest.mark.parametrize("field_name", ["start", "end"])
def test_shard_spec_carries_range(field_name: str) -> None:
    """ShardSpec must carry the range across the process boundary.

    Without it, --start is silently ignored whenever the proxy pool is full.
    """
    import pickle

    from yfin.pipeline.shard import ShardSpec

    spec = ShardSpec(
        run_id=1,
        shard_index=0,
        dataset_names=("_ranged",),
        full_refresh=False,
        database=None,
        start=date(2020, 1, 1),
        end=date(2021, 12, 31),
        selector="exchange=IST",
    )
    revived = pickle.loads(pickle.dumps(spec))
    assert getattr(revived, field_name) == getattr(spec, field_name)
    assert revived.selector == "exchange=IST"
