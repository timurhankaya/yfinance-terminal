"""`merge_stats` -- sums two `WriteStats` per key.

`DiscoveryDataset.upsert` splits writes in two (ungated + gated) and
produces two separate stats objects; `_record_items` expects a single
`WriteStats`. No helper for this existed in the codebase.
"""

from __future__ import annotations

from yfin.datasets.base import merge_stats
from yfin.storage.contracts import WriteStats


def _stats(
    attempted: dict[str, int] | None = None,
    verified: dict[str, int] | None = None,
    skipped: dict[str, int] | None = None,
) -> WriteStats:
    return WriteStats(
        attempted=dict(attempted or {}),
        verified=dict(verified or {}),
        skipped=dict(skipped or {}),
    )


def test_disjoint_tables_are_unioned() -> None:
    left = _stats(attempted={"news": 3}, verified={"news": 3})
    right = _stats(attempted={"search_quotes": 5}, verified={"search_quotes": 5})

    merged = merge_stats(left, right)

    assert merged.attempted == {"news": 3, "search_quotes": 5}
    assert merged.verified == {"news": 3, "search_quotes": 5}


def test_shared_table_counts_are_summed() -> None:
    """If the same table appears on both sides, the counts are summed.

    This doesn't happen today in `DiscoveryDataset` (the sets are
    disjoint), but the contract doesn't forbid it; silently dropping one
    side would corrupt the audit.
    """
    merged = merge_stats(_stats(attempted={"symbols": 2}), _stats(attempted={"symbols": 3}))
    assert merged.attempted == {"symbols": 5}


def test_zero_entries_are_preserved() -> None:
    """A zero-valued entry is not dropped.

    When the hash matches, `AsOfGate.upsert` writes
    `attempted.setdefault(table, 0)` for a target that carries no row
    (asof_base.py). If that entry were lost, the table would fall outside
    `stats.tables()`, `_record_items` would never see it, and it would
    drop out of that run's audit.
    """
    merged = merge_stats(_stats(attempted={"fund_top_holdings": 0}), _stats())
    assert "fund_top_holdings" in merged.attempted
    assert merged.attempted["fund_top_holdings"] == 0
    assert "fund_top_holdings" in merged.tables()


def test_skipped_is_not_double_counted() -> None:
    """`gated_result` is constructed with `skipped={}`.

    The caller seeds `result.skipped` only into the outer `stats`. Seeding
    both sides would turn this test red and double-count the `rows_skipped`
    audit.
    """
    outer = _stats(skipped={"search_quotes": 7})
    inner = _stats()  # skipped is not passed to the gated side

    assert merge_stats(outer, inner).skipped == {"search_quotes": 7}


def test_inputs_are_not_mutated() -> None:
    left = _stats(attempted={"news": 1})
    right = _stats(attempted={"news": 2})

    merge_stats(left, right)

    assert left.attempted == {"news": 1}
    assert right.attempted == {"news": 2}


def test_empty_merge_is_empty() -> None:
    merged = merge_stats(_stats(), _stats())
    assert merged.tables() == []
