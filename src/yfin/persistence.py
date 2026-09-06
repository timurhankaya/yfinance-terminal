"""PostgreSQL yazma mekanigi (PG S4).

Bu modul, dataset sozlesmesinden (datasets/base.py) AYRIDIR: sozlesme
hangi verinin nereye yazilacagini tanimlar, buradaki kod bunu
PostgreSQL'e nasil yazacagini bilir. Dataset'ler `RowWriter` protokolune bagimlidir,
SQLAlchemy'ye degil.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from sqlalchemy import Table, and_, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from yfin.datasets.base import TableWrite, WriteStats
from yfin.models.base import Base

# Cok anahtarli dogrulamada IN listesi cok uzayabilir; parcalara bolunur
VERIFY_CHUNK = 500

# Tek INSERT'e giren azami satir. bars_1m ilk dolumda sembol basina
# ~20.000 satir uretir (PB S6.7). Gerekce PAKET BOYUTU DEGILDIR
# (PostgreSQL'de boyle bir sinir yok). Iki gercek gerekce:
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

    Somut PostgreSQL detaylari (ON CONFLICT, anahtar varligi
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


def dedupe_rows(
    rows: list[dict[str, Any]],
    key_columns: tuple[str, ...],
    monotonic_columns: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Ayni anahtardan yalnizca bir satir birakir (son kazanir).

    ZORUNLUDUR: PostgreSQL `ON CONFLICT DO UPDATE` ayni komutta ayni
    satira IKI KEZ dokunamaz (ERROR 21000, "cannot affect row a second
    time"). MySQL `ON DUPLICATE KEY UPDATE` bunu sorunsuz yutuyordu, bu
    yuzden dataset'lerin cogunda dilim ici tekillik garantisi YOKTUR --
    57 TableWrite cagrisinin yalnizca dordu kendi icinde drop_duplicates
    yapiyor.

    `monotonic_columns` ISTISNADIR: grup icindeki EN BUYUK deger alinir.
    `GREATEST` yalnizca MEVCUT DB satiriyla yeni satiri karsilastirir,
    ayni batch'teki iki satiri DEGIL; duz "son kazanir" monotonikligi
    dilim icinde geri yazardi.

    Kaynak sirasi (ilk gorulme) korunur.
    """
    if len(rows) < 2:
        return rows
    seen: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key = tuple(row.get(name) for name in key_columns)
        current = seen.get(key)
        if current is None:
            seen[key] = dict(row)
            continue
        merged = {**current, **row}
        for col in monotonic_columns:
            old, new = current.get(col), row.get(col)
            if old is None:
                merged[col] = new
            elif new is None:
                merged[col] = old
            else:
                merged[col] = max(old, new)
        seen[key] = merged
    if len(seen) == len(rows):
        return rows
    return list(seen.values())


class PostgresRowWriter:
    """RowWriter'in PostgreSQL uygulamasi."""

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
        # dedupe de DILIMLEMEDEN ONCE: tekrarli iki anahtar farkli
        # dilimlere duserse ERROR 21000 CIKMAZ ama ikinci dilim
        # birincinin yazdigini ezer -- yani sessiz veri kaybi.
        rows = dedupe_rows(rows, write.key_columns, write.monotonic_columns)
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
        """Tek dilimin INSERT ... ON CONFLICT ifadesi.

        `present` TUM satirlardan turetilir ve disaridan gelir; dilimden
        hesaplansaydi align_rows'un is birligi bozulurdu.

        `index_elements` KUME OLARAK TAM ESLESMELIDIR: alt kume de ust
        kume de "there is no unique or exclusion constraint matching the
        ON CONFLICT specification" hatasi verir (sira onemsizdir).
        `key_columns`in gercek bir PK/UNIQUE'e karsilik geldigi
        test_persistence_contract.py'de invaryant olarak korunur.
        """
        stmt = pg_insert(table).values(rows)
        # Guncelleme kapsami INSERT'te yer alan kolonlara indirilir: kolon
        # seti sembole gore degisir (ornegin fon olmayan sembolde
        # 'Capital Gains' yok).
        update_map: dict[str, Any] = {}
        for col in write.update_columns:
            if col not in present:
                continue
            if col in write.monotonic_columns:
                # Kaynak ayni satir icin bir kez 1, ertesi kez 0
                # bildirebilir (repair heuristikleri pencere uzunluguna
                # baglidir); GREATEST bilgiyi geri yazmaz.
                # PostgreSQL GREATEST NULL'i YOK SAYAR (MySQL NULL
                # dondururdu). Burada DAHA GUVENLIDIR: kaynak bir kez NULL
                # bildirse bile mevcut deger korunur.
                update_map[col] = func.greatest(table.c[col], stmt.excluded[col])
            else:
                update_map[col] = stmt.excluded[col]
        if update_map:
            return stmt.on_conflict_do_update(
                index_elements=list(write.key_columns), set_=update_map
            )
        # Hicbir kolon guncellenmiyorsa satir sadece eklenir; mevcutsa
        # dokunulmaz. MySQL'de `ON DUPLICATE KEY UPDATE` bos olamadigi icin
        # `first_key = first_key` hilesi gerekiyordu; PostgreSQL'de
        # DO NOTHING var.
        return stmt.on_conflict_do_nothing(index_elements=list(write.key_columns))

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

        Etkilenen satir sayisi dogrulama icin KULLANILMAZ: `ON CONFLICT
        DO NOTHING` cakisma nedeniyle ATLANAN satiri saymaz (olculdu:
        INSERT 0 0). Anahtar varligi sorgusu daha guclu bir garanti
        verir -- "kac satir dokunuldu"yu degil, "istenen anahtarlarin
        kaci GERCEKTEN tabloda" sorusunu cevaplar; yani yazma-sonrasi
        BAGIMSIZ bir okumadir.
        """
        table = self._table(write.table)
        cols = [table.c[name] for name in write.key_columns]

        if len(cols) == 1:
            values = {row[write.key_columns[0]] for row in write.rows}
            stmt = select(func.count()).select_from(table).where(cols[0].in_(values))
            return int(self._session.execute(stmt).scalar_one())

        # Satir-kurucu IN: OR/AND bloklarindan belirgin sekilde hizli ve
        # PRIMARY KEY indeksini kullanan ayni erisim planini uretir.
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
