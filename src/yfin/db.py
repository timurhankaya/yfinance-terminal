"""Engine, session factory ve PostgreSQL advisory lock."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, text

from yfin.config import Settings, get_settings

SYNC_LOCK_NAME = "yfin_sync"


def _lock_key(name: str) -> int:
    """Ad -> imzali 64-bit advisory kilit anahtari.

    PostgreSQL'in `hashtext()`'i KULLANILMAZ: dokumante edilmemis bir ic
    fonksiyondur ve donus degeri surumler arasinda degisebilir. Anahtarin
    surumden bagimsiz olmasi, farkli sunucu surumlerine karsi kosan iki
    istemcinin AYNI kilidi gormesi icin gereklidir.
    """
    digest = hashlib.blake2b(name.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def _lock_key_parts(key: int) -> tuple[int, int]:
    """`pg_locks.classid` / `objid` -- IKISI DE `oid`, ISARETSIZ 32-bit.

    Ayristirma SQL'de DEGIL burada yapilir. SQL'de `(:key >> 32)::int`
    yazilsaydi iki ayri hata cikardi:
      1. Alt 32 bit 2^31'i astiginda `::int` ERROR 22003 verir.
      2. Negatif anahtarda `>>` aritmetik kaydirmadir, isareti uzatir ve
         ust 32 biti VERMEZ.
    'yfin_sync' anahtari HER IKI kosulu da saglar (olculdu: anahtar
    negatif, classid = 3 089 743 290), yani hata TEORIK DEGILDIR --
    projenin tek advisory kilidinde ilk kullanimda patlardi. Ustelik bu,
    LockNotAcquired hata yolunun ICINDE oldugu icin asil hatayi
    maskelerdi.
    """
    unsigned = key & 0xFFFF_FFFF_FFFF_FFFF
    return unsigned >> 32, unsigned & 0xFFFF_FFFF


def create_db_engine(
    settings: Settings | None = None,
    database: str | None = None,
    *,
    schema: str | None = None,
    pool_size: int | None = None,
    application_name: str = "yfin",
) -> Engine:
    """Havuz, es zamanli tuketicilere gore boyutlandirilir.

    Ayni anda baglanti isteyenler: ana thread'in sembol transaction'i,
    UC salt-okunur saglayici (watermark, scope, gap) ve advisory lock'u
    tutan oturum. Varsayilan pool_size=5 bu toplamda sinirda kalip
    QueuePool timeout uretebiliyordu.
    """
    cfg = settings or get_settings()
    # Shard'lar acik pool_size ile kurar (P4.12): gercek es zamanli
    # tuketici process basina DORTTUR. Uc salt-okunur saglayici kendi
    # threading.Lock'lariyla okumalari serilestirir, yani her biri EN
    # FAZLA bir baglanti tutar - WatermarkReader, ScopeReader ve
    # GapReader (PB S6.3, S6.5a) - arti ana thread'in sembol
    # transaction'i.
    # Invariant: process basina UST SINIR pool_size + max_overflow'dur,
    # yani 2 x pool_size. Varsayilan pool_size = max(5, workers+4) = 8
    # (workers=4) oldugundan shard basina en fazla 16, N shard icin
    # (N x 16) + 2 baglanti. PostgreSQL max_connections buna gore
    # ayarlanmalidir (docker-compose.yml: 200).
    if pool_size is None:
        pool_size = max(5, cfg.yf_max_workers + 4)

    # `options` TEK BIR DIZEDIR. timezone ve search_path ayri
    # connect_args anahtarlari olarak verilseydi biri digerini ezerdi.
    options = ["-c timezone=UTC"]
    if schema is not None:
        # `public` ZORUNLUDUR: timescaledb eklentisi oraya kurulur ve
        # `create_hypertable` / `timescaledb_information.*` aksi halde
        # cozulemez ("function by_range(unknown, interval) does not
        # exist" -- olculdu). Testler bunu fark etmeden duz tabloya
        # duserdi (S9.1).
        options.append(f"-c search_path={schema},public")

    return create_engine(
        cfg.db_url(database),
        pool_pre_ping=True,
        pool_recycle=3600,
        pool_size=pool_size,
        max_overflow=pool_size,
        pool_timeout=60,
        future=True,
        connect_args={
            # Kilidi kimin tuttugunu teshis edilebilir kilar (S5.3).
            # Shard'lar kendi indekslerini ekler (`yfin-shard-2`).
            "application_name": application_name,
            "options": " ".join(options),
        },
    )


class LockNotAcquired(RuntimeError):
    """Advisory kilit alinamadi; baska bir sync calisiyor."""


_HOLDER_SQL = text(
    "SELECT a.pid, a.application_name, a.client_addr, a.state, "
    "       left(a.query, 120) AS query "
    "  FROM pg_locks l "
    "  JOIN pg_stat_activity a ON a.pid = l.pid "
    " WHERE l.locktype = 'advisory' "
    "   AND l.classid = :classid AND l.objid = :objid "
    # objsubid = 1 tek-bigint bicimidir; iki-int bicimi 2 kullanir
    # (olculdu). Yanlis objsubid ile sorgu SESSIZCE bos doner.
    "   AND l.objsubid = 1 AND l.granted"
)


def lock_holder(conn: Any, name: str = SYNC_LOCK_NAME) -> str | None:
    """Kilidi tutan oturumun insan okunur tarifi (yoksa None).

    MySQL'de `IS_USED_LOCK` yalnizca bir connection id donduruyordu ve
    hata mesaji kullaniciya `SHOW PROCESSLIST` tavsiye ediyordu.
    PostgreSQL'de pid, application_name ve calisan sorgu DOGRUDAN alinir.
    """
    classid, objid = _lock_key_parts(_lock_key(name))
    row = conn.execute(_HOLDER_SQL, {"classid": classid, "objid": objid}).first()
    if row is None:
        return None
    return (
        f"pid={row.pid} application_name={row.application_name!r} "
        f"client={row.client_addr} state={row.state} query={row.query!r}"
    )


@contextmanager
def advisory_lock(engine: Engine, name: str = SYNC_LOCK_NAME) -> Iterator[None]:
    """pg_try_advisory_lock; alinamazsa LockNotAcquired.

    `timeout` PARAMETRESI YOKTUR. MySQL imzasinda vardi ama tum cagiranlar
    0 veriyordu (olculdu: bes cagiranin hicbiri deger gecmiyor) ve
    PostgreSQL'de karsiligi saniye cinsinden bir sure DEGIL, ikili bir
    secimdir -- `pg_advisory_lock` (sonsuz bekleme) ya da
    `pg_try_advisory_lock` (hic beklememe). Yaniltici bir parametreyi
    tasimak legacy birakmak olurdu.

    KAPSAM FARKI: PostgreSQL advisory kilitleri VERITABANI kapsamlidir;
    MySQL GET_LOCK sunucu genelindeydi. Farkli veritabanlarindaki iki
    kosu birbirini GORMEZ. Bu yuzden live testler ve `run_sync` AYNI
    veritabanini kullanmalidir (S5.2.1).

    Kilit oturum kapsamlidir, bu yuzden ayni baglanti sonuna kadar tutulur.
    """
    key = _lock_key(name)
    conn = engine.connect()
    try:
        got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar()
        if not got:
            # KIMIN tuttugu mesaja KONUR. Ciplak "alinamadi" mesaji, iki
            # kosuyu yanlislikla ust uste baslatan birine hicbir sey
            # soylemiyor.
            holder = lock_holder(conn, name) or "sahip bulunamadi"
            raise LockNotAcquired(
                f"advisory lock alinamadi: {name} ({holder}). "
                "Baska bir sync/live test kosusu devam ediyor olabilir."
            )
        try:
            yield
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
    finally:
        conn.close()
