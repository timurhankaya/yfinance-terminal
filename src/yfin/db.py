"""Engine, session factory ve MySQL advisory lock."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text

from yfin.config import Settings, get_settings

SYNC_LOCK_NAME = "yfin_sync"


def create_db_engine(
    settings: Settings | None = None,
    database: str | None = None,
    *,
    pool_size: int | None = None,
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
    # transaction'i. (Bu yorum once "IKIDIR" diyordu; price_bars iki yeni
    # okuyucu ekledi ve sayi guncellenmemisti.)
    # Invariant: process basina UST SINIR pool_size + max_overflow'dur,
    # yani 2 x pool_size. Varsayilan pool_size = max(5, workers+4) = 8
    # (workers=4) oldugundan shard basina en fazla 16, N shard icin
    # (N x 16) + 2 baglanti. Dort es zamanli tuketici bu tavanin cok
    # altindadir; formul degistirilmedi. MySQL max_connections buna gore
    # ayarlanmalidir.
    if pool_size is None:
        pool_size = max(5, cfg.yf_max_workers + 4)
    return create_engine(
        cfg.db_url(database),
        pool_pre_ping=True,
        pool_recycle=3600,
        pool_size=pool_size,
        max_overflow=pool_size,
        pool_timeout=60,
        future=True,
        # sql_mode SABITLENIR: gevsek sql_mode'lu bir sunucuda aralik disi
        # DECIMAL ve uzun VARCHAR degerleri SESSIZCE clamp/truncate edilir
        # (ERROR 1264 yerine yalnizca Warning). Bu hattin butun "satiri
        # dusur, hucreyi dusurme" mantigi hatanin GORULMESINE dayanir.
        connect_args={"sql_mode": "STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION"},
    )


class LockNotAcquired(RuntimeError):
    """GET_LOCK basarisiz; baska bir sync calisiyor."""


@contextmanager
def advisory_lock(engine: Engine, name: str = SYNC_LOCK_NAME, timeout: int = 0) -> Iterator[None]:
    """MySQL GET_LOCK; alinamazsa LockNotAcquired.

    Kilit oturum kapsamlidir, bu yuzden ayni baglanti sonuna kadar tutulur.
    """
    conn = engine.connect()
    try:
        got = conn.execute(text("SELECT GET_LOCK(:n, :t)"), {"n": name, "t": timeout}).scalar()
        if got != 1:
            # KIMIN tuttugu mesaja KONUR. Ciplak "alinamadi" mesaji, iki
            # kosuyu yanlislikla ust uste baslatan birine hicbir sey
            # soylemiyor - ayni sunucudaki baska bir sync mi, ayni
            # makinedeki ikinci bir pytest mi, bilinmiyordu. connection id
            # ile `SHOW PROCESSLIST` tek adimda cevap verir.
            holder = conn.execute(text("SELECT IS_USED_LOCK(:n)"), {"n": name}).scalar()
            raise LockNotAcquired(
                f"advisory lock alinamadi: {name} "
                f"(connection {holder} tutuyor; 'SHOW PROCESSLIST' ile bakin). "
                "Baska bir sync/live test kosusu devam ediyor olabilir."
            )
        try:
            yield
        finally:
            conn.execute(text("SELECT RELEASE_LOCK(:n)"), {"n": name})
    finally:
        conn.close()
