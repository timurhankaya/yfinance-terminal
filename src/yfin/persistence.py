"""MySQL yazma mekanigi (S7.2, S8.6).

Bu modul, dataset sozlesmesinden (datasets/base.py) AYRIDIR: sozlesme
hangi verinin nereye yazilacagini tanimlar, buradaki kod bunu MySQL'e
nasil yazacagini bilir. Dataset'ler `RowWriter` protokolune bagimlidir,
SQLAlchemy'ye degil.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from sqlalchemy import Table, and_, func, select, tuple_
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from yfin.datasets.base import TableWrite, WriteStats
from yfin.models.base import Base

# Cok anahtarli dogrulamada IN listesi cok uzayabilir; parcalara bolunur
VERIFY_CHUNK = 500

# Tek INSERT'e giren azami satir. bars_1m ilk dolumda sembol basina
# ~20.000 satir uretir (PB S6.7). Gerekce PAKET BOYUTU DEGILDIR -
# max_allowed_packet 64 MB, bu sekildeki 20.000 satir ~3-4 MB'dir. Iki
# gercek gerekce:
#   1. Kilit suresi: tek dev INSERT shard'lar arasi kilit suresini uzatir
#      ve _persist_with_retry'nin yeniden deneme penceresini buyutur.
#   2. Ya hep ya hic: kismi basarisizlikta 20.000 satirin tamami geri
#      alinir; parcali yazim coken bir kosudan sonra elde daha cok veri
#      birakir.
# .env anahtari DEGILDIR: persistence yfin.config'ten hicbir sey import
# etmez ve bu katmana yapilandirma bagimliligi sokmak istenmedi.
INSERT_CHUNK = 2000


class RowSink(Protocol):
    """Yalnizca yazma yetenegi (ISP).

    `apply_write` ve saf upsert yapan dataset'ler bundan fazlasina
    ihtiyac duymaz; hash okuma ya da sembol arama gerektirmeyen kod bu dar
    arayuze baglanir.
    """

    def write(self, write: TableWrite) -> int:
        """Satirlari yazar ve DOGRULANMIS satir sayisini dondurur."""
        ...


class HashReader(Protocol):
    """Snapshot/hash kapisi icin okuma yetenegi (ISP)."""

    def current_hash(self, table: str, key: Mapping[str, Any]) -> str | None:
        """Verilen anahtardaki mevcut content_hash (yoksa None).

        Anahtar cok kolonlu olabilir: ticker_calendar (symbol),
        market_status (region), market_summary (region, board_code),
        financial_periods (symbol, statement, freq, period_end).
        """
        ...


class SymbolLookup(Protocol):
    """Evren disi sembolleri isaretlemek icin arama yetenegi (ISP)."""

    def known_symbols(self, candidates: set[str]) -> set[str]:
        """Verilenlerden `symbols` tablosunda bulunanlar."""
        ...


class SnapshotWriter(RowSink, HashReader, Protocol):
    """Snapshot ve hash kapili dataset'lerin gordugu birlesim."""


class RowWriter(RowSink, HashReader, SymbolLookup, Protocol):
    """Dataset sozlesmesinin gordugu tam arayuz.

    Somut MySQL detaylari (ON DUPLICATE KEY UPDATE, anahtar varligi
    sorgusu) bu protokolun arkasinda kalir. `Dataset.upsert` imzasi
    LSP geregi TAM arayuzu alir; ic yardimcilar ise ihtiyac duyduklari
    DAR protokole baglanir (RowSink / SnapshotWriter).
    """


def apply_write(writer: RowSink, write: TableWrite, stats: WriteStats) -> None:
    """Tek bir TableWrite'i uygular ve istatistikleri gunceller."""
    stats.attempted[write.table] = stats.attempted.get(write.table, 0) + len(write.rows)
    stats.verified[write.table] = stats.verified.get(write.table, 0) + writer.write(write)


def align_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tum satirlari ayni kolon setine hizalar.

    SQLAlchemy cok satirli INSERT'te ilk satirin anahtarlarini kullanir;
    satirlar arasinda farkli kolonlar varsa geri kalani sessizce duser.
    """
    columns: dict[str, None] = {}
    for row in rows:
        for key in row:
            columns[key] = None
    if all(len(row) == len(columns) for row in rows):
        return rows
    return [{col: row.get(col) for col in columns} for row in rows]


class MySQLRowWriter:
    """RowWriter'in MySQL uygulamasi."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @staticmethod
    def _table(name: str) -> Table:
        return Base.metadata.tables[name]

    def write(self, write: TableWrite) -> int:
        table = self._table(write.table)

        # Silme, rows bos olsa da yapilir: "kapsam bosaldi" durumunda erken
        # cikilsaydi eski satirlar kalici olarak kalirdi (S6.2).
        if write.mode == "replace_scope":
            self._delete_scope(table, write)

        if not write.rows:
            return 0

        # align_rows TUM listeye, DILIMLEMEDEN ONCE uygulanir. Sonra
        # uygulansaydi her dilim farkli bir kolon setiyle ve farkli bir
        # ON DUPLICATE KEY UPDATE haritasiyla yazilirdi: ilk dilimde
        # bulunan bir kolon, ikincide hic gorunmedigi icin guncelleme
        # kapsamindan SESSIZCE duserdi.
        rows = align_rows(write.rows)
        present = set(rows[0])
        for start in range(0, len(rows), INSERT_CHUNK):
            self._session.execute(
                self._insert_stmt(table, rows[start : start + INSERT_CHUNK], write, present)
            )

        return self._verify(write)

    def _insert_stmt(
        self,
        table: Table,
        rows: list[dict[str, Any]],
        write: TableWrite,
        present: set[str],
    ) -> Any:
        """Tek dilimin INSERT ... ON DUPLICATE KEY UPDATE ifadesi.

        `present` TUM satirlardan turetilir ve disaridan gelir; dilimden
        hesaplansaydi align_rows'un is birligi bozulurdu.
        """
        stmt = mysql_insert(table).values(rows)
        # ON DUPLICATE KEY UPDATE yalnizca INSERT'te yer alan kolonlara
        # referans verebilir (ERROR 1054). Kolon seti sembole gore
        # degistiginden (ornegin fon olmayan sembolde 'Capital Gains' yok)
        # kapsam kesisime indirilir.
        update_map: dict[str, Any] = {}
        for col in write.update_columns:
            if col not in present:
                continue
            if col in write.monotonic_columns:
                # Kaynak ayni satir icin bir kez 1, ertesi kez 0
                # bildirebilir (repair heuristikleri pencere uzunluguna
                # baglidir); GREATEST bilgiyi geri yazmaz.
                update_map[col] = func.greatest(table.c[col], stmt.inserted[col])
            else:
                update_map[col] = stmt.inserted[col]
        if update_map:
            return stmt.on_duplicate_key_update(**update_map)
        # Hicbir kolon guncellenmiyorsa satir sadece eklenir; mevcutsa
        # dokunulmaz (ON DUPLICATE KEY UPDATE bos olamaz)
        first_key = write.key_columns[0]
        return stmt.on_duplicate_key_update(**{first_key: stmt.inserted[first_key]})

    def _delete_scope(self, table: Table, write: TableWrite) -> None:
        """replace_scope kapsamini siler.

        Kapsam scope_columns ile tanimlidir (varsayilan ("symbol",)):
        company_officers sembol, financial_facts ise
        (symbol, statement, freq, period_end) kapsaminda calisir.
        """
        cols = [table.c[name] for name in write.scope_columns]
        if write.scope_values is not None:
            scopes: list[Mapping[str, Any]] = list(write.scope_values)
        else:
            seen: dict[tuple[Any, ...], Mapping[str, Any]] = {}
            for row in write.rows:
                key = tuple(row[name] for name in write.scope_columns)
                seen[key] = {name: row[name] for name in write.scope_columns}
            scopes = list(seen.values())
        if not scopes:
            return

        if len(cols) == 1:
            values = [scope[write.scope_columns[0]] for scope in scopes]
            self._session.execute(table.delete().where(cols[0].in_(values)))
            return
        values_multi = [tuple(scope[name] for name in write.scope_columns) for scope in scopes]
        self._session.execute(table.delete().where(tuple_(*cols).in_(values_multi)))

    def _verify(self, write: TableWrite) -> int:
        """Anahtar varligi sorgusu (S8.6).

        ON DUPLICATE KEY UPDATE'in ROW_COUNT() degeri dogrulama icin
        KULLANILAMAZ: yeni satir 1, guncellenen 2, DEGISMEYEN 0 doner;
        ustelik deger CLIENT_FOUND_ROWS bayragina bagli oldugu icin surucu
        konfigurasyonuna gore degisir.
        """
        table = self._table(write.table)
        cols = [table.c[name] for name in write.key_columns]

        if len(cols) == 1:
            values = {row[write.key_columns[0]] for row in write.rows}
            stmt = select(func.count()).select_from(table).where(cols[0].in_(values))
            return int(self._session.execute(stmt).scalar_one())

        # Satir-kurucu IN: OR/AND bloklarindan 4,4x hizli ve ayni erisim
        # planini (type=range, key=PRIMARY) uretir. OR bicimi ayrica
        # range_optimizer_max_mem_size'a baglidir; asilirsa plan sessizce
        # ref'e duser.
        keys = [tuple(row[name] for name in write.key_columns) for row in write.rows]
        total = 0
        for start in range(0, len(keys), VERIFY_CHUNK):
            stmt = (
                select(func.count())
                .select_from(table)
                .where(tuple_(*cols).in_(keys[start : start + VERIFY_CHUNK]))
            )
            total += int(self._session.execute(stmt).scalar_one())
        return total

    def current_hash(self, table: str, key: Mapping[str, Any]) -> str | None:
        target = self._table(table)
        stmt = select(target.c["content_hash"]).where(
            and_(*(target.c[name] == value for name, value in key.items()))
        )
        value = self._session.execute(stmt).scalar_one_or_none()
        return str(value) if value is not None else None

    def known_symbols(self, candidates: set[str]) -> set[str]:
        from yfin.models.symbols import Symbol

        if not candidates:
            return set()
        stmt = select(Symbol.symbol).where(Symbol.symbol.in_(candidates))
        return set(self._session.execute(stmt).scalars())
