"""SQ S6.2.1: `merge_stats` -- iki `WriteStats`i anahtar bazinda toplar.

`DiscoveryDataset.upsert` yazimlari IKIYE bolup (kapi disi + kapili) iki ayri
istatistik uretir; `_record_items` tek bir `WriteStats` bekler. Kod tabaninda
bu yardimcinin karsiligi YOKTU.
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
    """Ayni tablo iki tarafta da gorunurse sayilar TOPLANIR.

    Bugun `DiscoveryDataset`te olmaz (kumeler ayriktir) ama sozlesme bunu
    yasaklamiyor; sessizce birini dusurmek denetimi bozardi.
    """
    merged = merge_stats(_stats(attempted={"symbols": 2}), _stats(attempted={"symbols": 3}))
    assert merged.attempted == {"symbols": 5}


def test_zero_entries_are_preserved() -> None:
    """Sifir degerli giris DUSURULMEZ.

    `AsOfGate.upsert` hash esitken satir TASIMAYAN hedefe
    `attempted.setdefault(table, 0)` yaziyor (asof_base.py). O giris
    kaybolursa tablo `stats.tables()` disinda kalir, `_record_items` onu HIC
    gormez ve o kosuda denetimden duser.
    """
    merged = merge_stats(_stats(attempted={"fund_top_holdings": 0}), _stats())
    assert "fund_top_holdings" in merged.attempted
    assert merged.attempted["fund_top_holdings"] == 0
    assert "fund_top_holdings" in merged.tables()


def test_skipped_is_not_double_counted() -> None:
    """SQ S6.2.1: `gated_result` `skipped={}` ile kurulur.

    Cagiran taraf `result.skipped`i YALNIZ dis `stats`e tohumlar. Iki tarafa
    da verilseydi bu test kirmizi olurdu ve `rows_skipped` denetimi ikiye
    katlanirdi.
    """
    outer = _stats(skipped={"search_quotes": 7})
    inner = _stats()  # kapili tarafa skipped GECIRILMEZ

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
