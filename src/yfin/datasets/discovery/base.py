"""Kesif dataset'lerinin ortak tabani (SQ S6.2).

Iki sey yapar:

1. KAPI TABLOSUNU degistirir. `asof_state` KULLANILAMAZ (SQ K3a): o
   tablonun `symbol` kolonu `symbol_fk_column` ile tanimlidir, yani
   `symbols.symbol`a `ON DELETE RESTRICT` FK tasir. Serbest arama terimi
   (`"Turkish Airlines"`) `symbols`ta YOKTUR ve kapi satiri FK ihlali (23503)
   alirdi. Uzunluk siniri bu sorunu COZMEZ. SI ayni duvara carpip
   `domain_asof_state`i acmisti; burada `discovery_asof_state` acilir ve
   `AsOfGate`in `asof_gate_table` / `asof_gate_key_columns` /
   `gate_identity` uzatma noktalari kullanilir -- YENI BIR KAPI SINIFI
   YAZILMAZ (SQ K3).

2. KAPI KAPSAMINI boler. `AsOfGate._gate_write` kapi satirinin
   `as_of_date` ve `fetched_at` alanlarini `_first_row(result)`tan, yani
   `writes` listesindeki ILK DOLU satirdan okur; `gate_identity` override'i
   da ayni satirdan `query_term` bekler. Dort tablo bu sozlesmeyi
   karsilamaz (asagida). "Siralamaya dikkat ederiz" YETMEZ: `search_quotes`
   bos kalabilirken `news` ya da `research_reports` dolu olabiliyor
   ("Turkish Airlines" olcumu) -- o durumda ilk dolu write onlardan biri
   olur ve siralama disiplini ise yaramaz.
"""

from __future__ import annotations

from typing import Any

from yfin.datasets.asof_base import AsOfDataset, _first_row
from yfin.datasets.base import NormalizedResult, WriteStats, merge_stats
from yfin.persistence import RowWriter, apply_write

DISCOVERY_GATE_TABLE = "discovery_asof_state"
DISCOVERY_GATE_KEY_COLUMNS = ("query_term", "dataset")

# Kapinin DISINDA kalan tablolar.
#
# `symbols` EVREN KAYDIDIR; `news`/`news_symbols`/`research_reports` ise
# KENDI kimlik uzayina sahip PAYLASILAN varliklardir. Bir arama teriminin
# icerik hash'i onlarin yazilip yazilmayacagini belirleyemez -- ayni haberi
# `Ticker.news` de, ayni raporu `Sector.research_reports` da yaziyor.
#
# Dordunun de ORTAK teknik ozelligi: `query_term` kolonu YOKTUR (ilk ucunde
# `as_of_date`/`fetched_at` de yoktur). Kapili tarafa girselerdi
# `_first_row` yanlis satiri dondurur ve kapi yazimi KeyError verirdi.
UNGATED_TABLES = frozenset({"symbols", "news", "news_symbols", "research_reports"})


class DiscoveryDataset[RawT](AsOfDataset[RawT]):
    asof_gate_table = DISCOVERY_GATE_TABLE
    asof_gate_key_columns = DISCOVERY_GATE_KEY_COLUMNS

    def gate_identity(self, result: NormalizedResult) -> dict[str, Any]:
        """Kapi anahtari: (query_term, dataset).

        Varsayilan `_first_row(result)["symbol"]` okurdu; burada kapsam
        sembol DEGIL terimdir -- bir arama teriminin sonucu birden cok
        sembol tasir.
        """
        return {"query_term": _first_row(result)["query_term"], "dataset": self.name}

    def upsert(self, writer: RowWriter, result: NormalizedResult) -> WriteStats:
        ungated = [w for w in result.writes if w.table in UNGATED_TABLES]
        gated = [w for w in result.writes if w.table not in UNGATED_TABLES]

        # 1. Kapisiz yazimlar ONCE: `search_report_hits`in FK'si
        #    `research_reports`e bakar ve ebeveyn once yazilmalidir.
        stats = WriteStats(skipped=dict(result.skipped))
        for write in ungated:
            apply_write(writer, write, stats)

        # 2. `skipped` BOS gecirilir. Iki gerekce:
        #    (a) dis `stats` onu zaten tohumladi; ikisi toplanirsa
        #        `rows_skipped` denetimi IKIYE KATLANIR.
        #    (b) `NormalizedResult.is_empty` "satir yok VE skipped bos"
        #        demektir (base.py). `gated` satirsiz ama `skipped` doluyken
        #        `is_empty` False olur, `_first_row` ValueError firlatir ve
        #        hucre HAKSIZ YERE `failed`a duserdi.
        gated_result = NormalizedResult(writes=gated, skipped={})
        return merge_stats(stats, super().upsert(writer, gated_result))
