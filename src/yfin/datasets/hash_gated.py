"""Hash kapili dataset tabani (S6.3/b).

`SnapshotDataset` bu is icin KULLANILAMAZ: orada karsilastirilan tablo
(`snapshot_table`) ile yazilan tablo (`history_table`) FARKLIDIR ve atlanan
sey gecmis tablosunun satirlaridir. Burada ise karsilastirilan tablo ile
yazilan tablo AYNIDIR (`financial_periods`) ve atlanan sey COCUK tablodur
(`financial_facts`).

Kural:
1. Hash sorgusu her seyden once yapilir.
2. Hash esitse cocuk satirlari hic yazilmaz -> skipped.
3. Baslik satiri HER DURUMDA yazilir; hash esitse yalnizca fetched_at
   guncellenir. Boylece fetched_at "son dogrulama zamani"dir.
"""

from __future__ import annotations

from typing import Any

from yfin.datasets.base import Dataset, NormalizedResult, TableWrite, WriteStats
from yfin.persistence import RowWriter, apply_write

# Hash degismediginde baslik satirinda yalnizca bu kolon guncellenir
UNCHANGED_UPDATE_COLUMNS = ("fetched_at",)


class HashGate:
    """Hash kapisi -- `Dataset` HIYERARSISINDEN BAGIMSIZ mixin (SQ S6.2).

    `AsOfGate`in SI S6.2'de ayristirilma gerekcesinin aynisi: kapi mantigi
    fetch/normalize IMZASINDAN bagimsizdir, ama sinif hiyerarsisi degildir.
    `HashGatedDataset` `Dataset[RawT]`in altindadir ve
    `fetch(SyncContext)` / `normalize(raw, symbol)` imzasini tasir; ekran
    tarafi ise `GlobalDataset`tir ve `fetch(MarketContext)` /
    `normalize(raw)` imzasini tasir. Iki hiyerarsi BIRLESTIRILEMEZ.

    Mixin ne fetch ne normalize imzasina dokunur; yalnizca `upsert` saglar.
    Davranis ayristirmadan ONCEKIYLE BIREBIR aynidir -- mevcut
    `financial_statements` testleri bunu surer.
    """

    gate_table: str
    child_table: str
    gate_key_columns: tuple[str, ...]

    def _key(self, row: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(row[name] for name in self.gate_key_columns)

    def upsert(self, writer: RowWriter, result: NormalizedResult) -> WriteStats:
        stats = WriteStats(skipped=dict(result.skipped))
        gate_writes = [w for w in result.writes if w.table == self.gate_table]
        child_writes = [w for w in result.writes if w.table == self.child_table]
        other_writes = [
            w for w in result.writes if w.table not in (self.gate_table, self.child_table)
        ]

        # 1. Hash sorgulari, HICBIR yazmadan once
        unchanged: set[tuple[Any, ...]] = set()
        changed: list[dict[str, Any]] = []
        for write in gate_writes:
            for row in write.rows:
                key = {name: row[name] for name in self.gate_key_columns}
                current = writer.current_hash(self.gate_table, key)
                if current == row["content_hash"]:
                    unchanged.add(self._key(row))
                else:
                    changed.append(row)

        # 2. Baslik satirlari: degisen tam, degismeyen yalnizca fetched_at
        for write in gate_writes:
            changed_rows = [row for row in write.rows if self._key(row) not in unchanged]
            unchanged_rows = [row for row in write.rows if self._key(row) in unchanged]
            if changed_rows:
                apply_write(writer, _with_rows(write, changed_rows), stats)
            if unchanged_rows:
                apply_write(
                    writer,
                    _with_rows(write, unchanged_rows, update_columns=UNCHANGED_UPDATE_COLUMNS),
                    stats,
                )

        # 3. Cocuk satirlari: yalnizca degisen donemler icin.
        # Silme kapsami satirlardan DEGIL, degisen baslik anahtarlarindan
        # turetilir; aksi halde donemin tum kalemleri NaN geldiginde (rows
        # bos) eski satirlar kalici olarak kalirdi.
        scope_values = tuple({name: row[name] for name in self.gate_key_columns} for row in changed)
        for write in child_writes:
            kept = [row for row in write.rows if self._key(row) not in unchanged]
            dropped = len(write.rows) - len(kept)
            if dropped:
                stats.skipped[write.table] = stats.skipped.get(write.table, 0) + dropped
            if not scope_values:
                # Hicbir donem degismedi: silme de yazma da yapilmaz
                stats.attempted.setdefault(write.table, 0)
                stats.verified.setdefault(write.table, 0)
                continue
            apply_write(
                writer,
                TableWrite(
                    table=write.table,
                    rows=kept,
                    key_columns=write.key_columns,
                    update_columns=write.update_columns,
                    mode="replace_scope",
                    scope_columns=self.gate_key_columns,
                    scope_values=scope_values,
                ),
                stats,
            )

        for write in other_writes:
            apply_write(writer, write, stats)
        return stats


class HashGatedDataset[RawT](HashGate, Dataset[RawT]):
    """Sembol ekseninin hash kapili dataset'i.

    Govde `HashGate`e tasindi (SQ S6.2); bu sinif yalnizca iki tarafi
    birlestirir ve mevcut cagirilari degistirmeden birakir.
    """


def _with_rows(
    write: TableWrite,
    rows: list[dict[str, Any]],
    *,
    update_columns: tuple[str, ...] | None = None,
) -> TableWrite:
    return TableWrite(
        table=write.table,
        rows=rows,
        key_columns=write.key_columns,
        update_columns=update_columns if update_columns is not None else write.update_columns,
        mode=write.mode,
        scope_columns=write.scope_columns,
        scope_values=write.scope_values,
    )
