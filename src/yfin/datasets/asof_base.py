"""as-of dataset tabani (AH S6.1, SI S6.2).

Kod tabaninda IKI hash kapisi zaten var; bu UCUNCU kardestir. Digerlerinin
neden kullanilamadigi:

- `SnapshotDataset`: karsilastirilan tablo (`snapshot_table`) ile yazilan
  tablo (`history_table`) FARKLIDIR ve atlanan sey gecmis tablosunun
  satirlaridir. Burada kapi bir VERI tablosu bile degildir ve 1-4 hedef
  tablo vardir.
- `HashGatedDataset`: kapi ile cocuk AYNI anahtar uzayindadir
  (`financial_periods` -> `financial_facts`) ve cocugun silme kapsami
  `gate_key_columns`'tan turetilir. Burada kapi anahtari
  (symbol, dataset)'tir; cocuklarin `dataset` diye bir kolonu YOKTUR ve
  silme kapsami cocugun kendi `scope_columns`'idir.

Paylasilan ilke aynen korunur: KAPI SATIRI HER DURUMDA YAZILIR, hash esitse
yalnizca `fetched_at` guncellenir; boylece `fetched_at` "son DOGRULAMA
zamani"dir, "son degisim zamani" degil.

SI S6.2 -- KAPI MANTIGI `Dataset` HIYERARSISINDEN AYRILDI. `AsOfGate` bir
mixin'dir; `AsOfDataset` (sembol tarafi) ve `DomainAsOfDataset` (sektor /
endustri tarafi) onu paylasir. Iki hiyerarsi BIRLESTIRILEMEZ: birinin
`fetch(SyncContext)` / `normalize(raw, symbol)`, digerinin
`fetch(DomainContext)` / `normalize(raw, key)` imzasi vardir. Mixin ne
fetch ne normalize imzasina dokunur; yalnizca `content_hash` + `upsert`
saglar.

`asof_gate_table` adi CIPLAK `gate_table` OLAMAZ: `HashGatedDataset` o adi
zaten FARKLI bir anlamda kullaniyor (orada kapi bir VERI tablosudur,
hash_gated.py:27-33). Kardes siniflarda ayni ad, farkli sozlesme sessiz bir
tuzak olurdu.
"""

from __future__ import annotations

from typing import Any

from yfin import normalize as nz
from yfin.datasets.base import Dataset, NormalizedResult, TableWrite, WriteStats
from yfin.datasets.hash_gated import UNCHANGED_UPDATE_COLUMNS
from yfin.persistence import RowWriter, apply_write

GATE_TABLE = "asof_state"
GATE_KEY_COLUMNS = ("symbol", "dataset")

# Domain (sektor / endustri) tarafinin kapisi (SI S5.7). Sembol tarafinin
# `asof_state`'i KULLANILAMAZ: oradaki anahtar (symbol, dataset)'tir ve
# domain veri tablolarindaki `symbol` SIRKETIN sembolu, domain'in degil.
DOMAIN_GATE_TABLE = "domain_asof_state"
DOMAIN_GATE_KEY_COLUMNS = ("domain_key", "dataset", "region")

# Bolgesiz domain dataset'lerinin kapi satirinda yazdigi isaretci
# (market_runner.GLOBAL_SCOPE_MARKER deseni).
GLOBAL_REGION_MARKER = "*"

# Hash'ten DISLANAN kolonlar. Ucu de her calistirmada degisir; govdeye
# girselerdi hash HICBIR ZAMAN esitlenmez ve mekanizma sessizce hic
# calismazdi: her gun her satir yeniden yazilir, kimse fark etmezdi.
#
# `first_seen_at` SI S6.2/3 ile eklendi. Bugun hicbir VERI tablosunda bu
# kolon yok (yalniz `asof_state`'te, models/asof.py:42), bu yuzden mevcut
# 13 as-of dataset'inin hash'i DEGISMEZ. Eklenmeseydi
# `research_reports.first_seen_at` her koşuda degisip hash govdesine
# girer ve kapi ASLA esitlenmezdi.
VOLATILE_COLUMNS = frozenset({"as_of_date", "fetched_at", "first_seen_at"})

# Kapi satirinda hash DEGISTIGINDE guncellenen kolonlar. `first_seen_at`
# BILINCLI olarak disaridadir: ON DUPLICATE KEY UPDATE onu kapsasaydi
# "ilk INSERT'te yazilir" kurali (AH S5.4) bozulurdu.
GATE_UPDATE_COLUMNS = ("as_of_date", "content_hash", "row_count", "fetched_at")


def asof_produces(*tables: str, gate: str = GATE_TABLE) -> tuple[str, ...]:
    """Hedef tablolar + KAPI TABLOSU (AH S6.1).

    `produces` sozlesmesi "yazdigi tablo adlari"dir ve `_failed_records`
    hata yolunda onu kullanir; kapi bildirilmezse `asof_state` satiri
    denetimden duser ve `produces` ile fiili cikti ayrisir. Yardimci
    BURADADIR: kapi tablosunun adini bilen tek modul budur.

    `gate` SI S6.2/4 ile eklendi; varsayilani degismedi, bu yuzden mevcut
    13 cagrinin ciktisi BIREBIR aynidir.
    """
    return (*tables, gate)


def _sort_key(row: dict[str, Any], key_columns: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(str(row.get(name)) for name in key_columns)


def _first_row(result: NormalizedResult) -> dict[str, Any]:
    """`writes` sirasindaki ILK veri satiri.

    `next(..., None)`: `upsert` bunu yalnizca `is_empty` FALSE iken cagirir,
    ama `is_empty` "satir yok VE skipped bos" demektir -- satirsiz ama
    `skipped` dolu bir sonuc StopIteration firlatirdi. Bugun ulasilamaz;
    sozlesme bunu yasaklamadigi icin acikca korunur.
    """
    first = next((row for write in result.writes for row in write.rows), None)
    if first is None:  # pragma: no cover - savunma
        raise ValueError("kapi satiri icin veri satiri yok")
    return first


class AsOfGate:
    """as-of kapisi -- `Dataset` hiyerarsisinden BAGIMSIZ mixin (SI S6.2).

    Alt sinif `name` ve `produces` saglar; mixin yalnizca `content_hash` ve
    `upsert` uretir. `gate_identity()` kapi satirinin ANAHTAR alanlarini
    dondurur ve varsayilani BUGUNKU davranistir.
    """

    # Alt sinif saglar. `produces` burada da BILDIRILIR: `prune.py`
    # kapsam turetmesini `AsOfGate` uzerinden yapiyor ve iki taraf
    # (`Dataset` / `DomainDataset`) ortak bir atadan gelmiyor.
    name: str
    produces: tuple[str, ...]
    asof_gate_table: str = GATE_TABLE
    asof_gate_key_columns: tuple[str, ...] = GATE_KEY_COLUMNS

    def gate_identity(self, result: NormalizedResult) -> dict[str, Any]:
        """Kapi satirinin anahtar alanlari (varsayilan = sembol tarafi)."""
        return {"symbol": _first_row(result)["symbol"], "dataset": self.name}

    def content_hash(self, result: NormalizedResult) -> str:
        """`result.writes`'in kanonik govdesinin SHA-256'si.

        Satirlar `key_columns`'a gore SIRALANIR. `nz.canonical_json` yalnizca
        sozluk anahtarlarini siralar (sort_keys=True); liste sirasi korunur.
        Kaynak -- Yahoo'nun "ilk 10 kurum" listesi, insider_roster, domain
        `topCompanies` -- sirayi degistirdiginde icerik ayniyken hash degisir
        ve mekanizma her gun gereksiz yazim yapardi.
        """
        payload: list[dict[str, Any]] = []
        for write in sorted(result.writes, key=lambda w: w.table):
            stripped: list[dict[str, Any]] = [
                {k: v for k, v in row.items() if k not in VOLATILE_COLUMNS}
                for row in write.rows
            ]
            stripped.sort(key=lambda row: _sort_key(row, write.key_columns))
            payload.append({"table": write.table, "rows": stripped})
        return nz.content_hash(canonical=nz.canonical_json(payload))

    def _gate_write(
        self,
        result: NormalizedResult,
        *,
        digest: str,
        unchanged: bool,
    ) -> TableWrite:
        first = _first_row(result)
        row = {
            **self.gate_identity(result),
            "as_of_date": first["as_of_date"],
            "content_hash": digest,
            "row_count": sum(len(w.rows) for w in result.writes),
            "first_seen_at": first["fetched_at"],
            "fetched_at": first["fetched_at"],
        }
        return TableWrite(
            table=self.asof_gate_table,
            rows=[row],
            key_columns=self.asof_gate_key_columns,
            update_columns=UNCHANGED_UPDATE_COLUMNS if unchanged else GATE_UPDATE_COLUMNS,
        )

    def upsert(self, writer: RowWriter, result: NormalizedResult) -> WriteStats:
        stats = WriteStats(skipped=dict(result.skipped))
        if result.is_empty:
            # Bos sonucta kapi satiri YAZILMAZ: aksi halde her fon-olmayan
            # sembol (ve her liste bloğu olmayan endustri) icin olu satir
            # birikir ve `first_seen_at` "ilk kez BOS donuldu" anlamina
            # kayardi (AH S6.1/3). Hucre `empty` olur.
            return stats

        digest = self.content_hash(result)
        identity = self.gate_identity(result)
        current = writer.current_hash(self.asof_gate_table, identity)
        unchanged = current == digest

        if unchanged:
            # Veri tablolarina YAZILMAZ. `skipped` yalnizca SATIR TASIYAN
            # tabloya yazilir; bos TableWrite tasiyan hedef `empty` kalir
            # (AH S7.2) -- BND'de fund_top_holdings 0 satirdir.
            for write in result.writes:
                if write.rows:
                    stats.skipped[write.table] = stats.skipped.get(write.table, 0) + len(
                        write.rows
                    )
                else:
                    # Satir TASIMAYAN hedef aksi halde hicbir sayacta yer
                    # almaz, `stats.tables()` disinda kalir ve
                    # `_record_items` onu HIC gormez -- tablo o
                    # calistirmada denetimden duserdi. Sifir attempted +
                    # sifir skipped = `empty` (AH S7.2): BND'de
                    # fund_top_holdings bostur, kardes uc tablo `skipped`.
                    stats.attempted.setdefault(write.table, 0)
                    stats.verified.setdefault(write.table, 0)
        else:
            for write in result.writes:
                apply_write(writer, write, stats)

        # Kapi satiri her iki dalda da yazilir ve SAYACLARA GIRER
        # (`apply_write`), tipki `HashGatedDataset`'in baslik satirinda
        # yaptigi gibi. Sayaclara girmeseydi `_failed_records` hata
        # durumunda `produces`'tan bir kapi satiri uretirken basari
        # durumunda hicbir satir olusmaz, denetim asimetrik kalirdi.
        apply_write(writer, self._gate_write(result, digest=digest, unchanged=unchanged), stats)
        return stats


class AsOfDataset[RawT](AsOfGate, Dataset[RawT]):
    """as_of_date PK'da; content_hash degismediyse VERI tablolarina yazilmaz.

    Davranis SI S6.2 ayristirmasindan sonra BIREBIR ayni kalir: mixin'in
    varsayilanlari (`GATE_TABLE`, `GATE_KEY_COLUMNS`, sembol kimligi)
    onceki gomulu degerlerin ta kendisidir.
    """
