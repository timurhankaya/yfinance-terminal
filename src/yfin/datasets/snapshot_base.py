"""Snapshot dataset'leri icin ortak taban (S6.3/a).

Snapshot tablosu satiri gunceller; _history tablosu YALNIZCA content_hash
degistiyse yeni satir ekler. Degismediginde bu bir 'skipped'tir, hata degil.

Karsilastirilan tablo (snapshot) ile yazilan tablo (_history) FARKLI oldugu
icin "once karsilastir, sonra yaz" sirasi guvenlidir. Ayni tabloya yazan
hash kapisi icin `HashGatedDataset` kullanilir.
"""

from __future__ import annotations

from typing import Any

from yfin.datasets.base import Dataset, NormalizedResult, TableWrite, WriteStats
from yfin.persistence import RowWriter, SnapshotWriter, apply_write


def snapshot_upsert(
    writer: SnapshotWriter,
    result: NormalizedResult,
    *,
    snapshot_table: str,
    history_table: str,
    key_columns: tuple[str, ...],
) -> WriteStats:
    """Snapshot + gecmis yazimi; sembol veya bolge anahtarli calisir."""
    stats = WriteStats(skipped=dict(result.skipped))
    history_writes: list[TableWrite] = []
    other_writes: list[TableWrite] = []

    for write in result.writes:
        (history_writes if write.table == history_table else other_writes).append(write)

    # Hash karsilastirmasi snapshot tablosu GUNCELLENMEDEN ONCE yapilir;
    # aksi halde karsilastirma her zaman esitlenir ve _history hicbir
    # zaman yeni satir almaz.
    keep: list[TableWrite] = []
    for write in history_writes:
        kept: list[dict[str, Any]] = []
        skipped = 0
        for row in write.rows:
            key = {name: row[name] for name in key_columns}
            current = writer.current_hash(snapshot_table, key)
            if current == row["content_hash"]:
                skipped += 1
            else:
                kept.append(row)
        if skipped:
            stats.skipped[write.table] = stats.skipped.get(write.table, 0) + skipped
        keep.append(
            TableWrite(
                table=write.table,
                rows=kept,
                key_columns=write.key_columns,
                update_columns=write.update_columns,
                mode=write.mode,
                scope_columns=write.scope_columns,
                scope_values=write.scope_values,
            )
        )

    for write in other_writes:
        apply_write(writer, write, stats)
    for write in keep:
        apply_write(writer, write, stats)

    return stats


class SnapshotDataset[RawT](Dataset[RawT]):
    """Iki tabloya yazar: guncel snapshot + gecmis.

    Hash karsilastirmasi DB okumasi gerektirdigi icin normalize() icinde
    degil, upsert() icinde yapilir; normalize saf kalir.
    """

    snapshot_table: str
    history_table: str
    # Snapshot tablosunun anahtar kolonlari: ticker_* icin ("symbol",),
    # market_status icin ("region",), market_summary icin
    # ("region", "board_code").
    key_columns: tuple[str, ...] = ("symbol",)

    def upsert(self, writer: RowWriter, result: NormalizedResult) -> WriteStats:
        return snapshot_upsert(
            writer,
            result,
            snapshot_table=self.snapshot_table,
            history_table=self.history_table,
            key_columns=self.key_columns,
        )
