# PostgreSQL 18 + TimescaleDB Geçişi — Uygulama Planı

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `yfin` veri hattının depolama motorunu MySQL 8'den PostgreSQL 18 + TimescaleDB'ye taşımak; kod tabanında sıfır MySQL referansı bırakmak.

**Architecture:** Dataset sözleşmesi (`datasets/base.py`) ve `RowWriter` protokolü motordan bağımsızdır ve DEĞİŞMEZ — `datasets/` altındaki 40+ dosyaya dokunulmaz. Değişim üç katmanda toplanır: (1) tip/şema katmanı (`models/`), (2) yazma ve kilit mekaniği (`persistence.py`, `db.py`, `runner.py`), (3) test altyapısı (süreç başına veritabanı → süreç başına şema). `price_bars` ve `price_history` elle yönetilen aylık partition yerine TimescaleDB hypertable olur.

**Tech Stack:** Python 3.13, SQLAlchemy 2.0, Alembic, psycopg 3, PostgreSQL 18.6, TimescaleDB 2.29.2, Docker Compose, pytest, ruff, mypy --strict.

**Spec:** `docs/superpowers/specs/2026-09-04-postgres-timescaledb-migration-design.md` (v2)

## Global Constraints

Bu kısıtlar HER görevin gereksinimlerine örtük olarak dahildir.

- **Sıfır MySQL referansı.** `src/`, `tests/`, `migrations/`, `scripts/`, `pyproject.toml`, `alembic.ini` içinde MySQL'e ait tip, fonksiyon, hata kodu, yorum veya gerekçe kalmaz. Nihai denetim Görev 13'tedir.
- **İmaj sabit:** `timescale/timescaledb:2.29.2-pg18`. `latest-pg18` KULLANILMAZ (değişken tag).
- **Veri taşınmaz.** Mevcut MySQL verisi kasıtlı olarak terk edilir. Veri göçü, çift yazma veya MySQL'e geri dönüş yolu yazılmaz.
- **`datasets/` altındaki 40+ dosya değişmez** — Görev 7'de adı geçen `datasets/symbols.py` ve `datasets/holders/insider_transactions.py` (yalnızca yorum) istisnadır.
- **Protokoller değişmez:** `RowSink`, `HashReader`, `SymbolLookup`, `SnapshotWriter`, `RowWriter` (`persistence.py`).
- **Yorum politikası (spec §13):** gerekçesi motora bağlı yorum silinir/yeniden yazılır; gerekçesi veriye bağlı yorum (ölçülen uzunluklar, sembol adları, DST notları) AYNEN korunur. Şüphede kalınırsa korunur.
- **Türkçe docstring/yorum ASCII'dir** (diakritiksiz) — mevcut kod konvansiyonu. Spec/plan dosyaları tam diakritiklidir.
- **Her görev `ruff check` ve `mypy --strict` temiz bitmelidir.**
- **`.env` `.gitignore`'dadır ve commit EDİLMEZ.** Yalnızca `.env.example` sürümlenir.

**Doğrulama komutları (her görevde kullanılır):**
```bash
.venv/bin/ruff check src tests
.venv/bin/mypy --strict
.venv/bin/pytest -q                 # unit (varsayilan -m 'not repo and not live')
.venv/bin/pytest -q -m repo         # gercek TimescaleDB gerektirir
```

---

## Görev sırası ve bağımlılıklar

```
1 (altyapi)
  -> 2 (tip katmani + MYSQL_TABLE_ARGS temizligi)
    -> 3 (modeller + timescale_ddl)
      -> 4 (view)
        -> 5 (persistence)
          -> 6 (runner/normalize/rescale)  \
          -> 7 (collation normalizasyonlari) > birbirinden bagimsiz
          -> 8 (maintenance silme)          /
            -> 9 (alembic)
              -> 10 (conftest)
                -> 11 (mekanik test uyarlamalari)
                  -> 12 (yeni testler)
                    -> 13 (yorum temizligi + paket meta + nihai denetim)
```

**İmport sağlığı görev sınırlarını belirledi.** İki yerde sıra pazarlık
edilemez:

- `MYSQL_TABLE_ARGS` **Görev 2'de** hem `base.py`'den hem 15 model
  dosyasından silinir. Model dosyaları Görev 3'e bırakılsaydı Görev 2'nin
  doğrulaması `ImportError` ile toplama aşamasında patlardı.
- `timescale_ddl()` **Görev 3'te** yazılır, Görev 9'da değil.
  `price_bars_partition_ddl` `models/__init__.py` ve `tests/conftest.py`
  tarafından import ediliyor; silinip yerine bir şey konmasaydı Görev
  3'ten Görev 9'a kadar ağaç import edilemez halde kalırdı.

Görev 9 öncesinde 2-4 bitmiş olmalıdır (migration modellerden üretilir).

---

### Görev 1: Altyapı — git tabanı, Docker, config, db.py

**Files:**
- Create: `docker-compose.yml`
- Modify: `.env.example` (DB bölümü)
- Modify: `src/yfin/config.py:13-18,102-116`
- Modify: `src/yfin/db.py` (tamamı)
- Modify: `pyproject.toml:18`
- Test: `tests/unit/test_lock_key.py` (yeni)

**Interfaces:**
- Consumes: —
- Produces:
  - `Settings.db_url(database: str | None = None) -> URL` — dialect `postgresql+psycopg`
  - `Settings.bootstrap_url() -> URL` — `postgres` bakım veritabanı, YALNIZCA `CREATE DATABASE` için
  - `yfin.db.create_db_engine(settings: Settings | None = None, database: str | None = None, *, schema: str | None = None, pool_size: int | None = None, application_name: str = "yfin") -> Engine`
  - `yfin.db._lock_key(name: str) -> int` — imzalı 64-bit
  - `yfin.db._lock_key_parts(key: int) -> tuple[int, int]` — `(classid, objid)`, ikisi de `oid` aralığında
  - `yfin.db.lock_holder(conn, name: str = SYNC_LOCK_NAME) -> str | None`
  - `yfin.db.advisory_lock(engine: Engine, name: str = SYNC_LOCK_NAME) -> Iterator[None]` — `timeout` parametresi YOK
  - `yfin.db.LockNotAcquired`, `yfin.db.SYNC_LOCK_NAME = "yfin_sync"`

- [ ] **Step 1: Mevcut MySQL halini git tabanı olarak kaydet**

Proje henüz git deposu değil. Bu ölçekte bir migrasyonda geri dönülebilir bir taban şart.

```bash
cd /Users/timurhan.kaya/Projects/learn/yfinance
git init
git add -A
git status --short | head -20   # .env, .venv, __pycache__ GORUNMEMELI
git commit -m "chore: MySQL 8 tabanli mevcut hal (migrasyon oncesi taban)"
```

Beklenen: `.env`, `.venv/`, `*.egg-info/`, `__pycache__/` commit'e girmemiş (`.gitignore` kapsıyor).

- [ ] **Step 2: `docker-compose.yml` yaz**

```yaml
services:
  timescaledb:
    image: timescale/timescaledb:2.29.2-pg18
    container_name: yfin-timescaledb
    environment:
      POSTGRES_USER: ${DB_USER:-yfin}
      POSTGRES_PASSWORD: ${DB_PASSWORD:?DB_PASSWORD gerekli}
      POSTGRES_DB: ${DB_NAME:-yfinance}
      TZ: UTC
      PGTZ: UTC
    command:
      - postgres
      - -c
      - max_connections=200
      - -c
      - timescaledb.max_background_workers=8
    ports: ["${DB_PORT:-5432}:5432"]
    # MOUNT `/var/lib/postgresql` -- `/data` ALT DIZINI DEGIL (PG18
    # konvansiyon degisikligi; eski yol Exited(1) verir, olculdu)
    volumes: ["yfin-pgdata:/var/lib/postgresql"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${DB_USER:-yfin} -d ${DB_NAME:-yfinance}"]
      interval: 5s
      timeout: 5s
      retries: 20
volumes:
  yfin-pgdata:
```

`max_connections=200` gerekçesi: `db.py` havuz formülü process başına `2 × max(5, workers+4)` tavanı koyar; `workers=4` ve `yf_max_shards=4` ile toplam `4×16 + 2 = 66`.

- [ ] **Step 3: `.env.example` ve yerel `.env` güncelle**

`.env.example` içindeki `DB_*` bölümü:

```
# --- veritabani ---
DB_HOST=localhost
DB_PORT=5432
DB_USER=yfin
# ZORUNLU: bos birakilamaz, docker compose ayaga kalkmaz
DB_PASSWORD=
DB_NAME=yfinance
DB_TEST_NAME=yfinance_test
```

Dosyanın geri kalanı (yfinance/proxy ayarları) DEĞİŞMEZ.

Yerel `.env` aynı anahtarlarla güncellenir; `DB_PASSWORD` değeri **kullanıcıdan istenir**, uygulayıcı üretmez.

- [ ] **Step 4: Konteyneri ayağa kaldır**

**MySQL DURDURULMAZ.** Plan v1'de "mevcut MySQL örneği durdurulur" ön koşulu vardı; uygulama başlangıcında ölçüldü ve **kaldırıldı**:

- PostgreSQL 5432, MySQL 3306 — **port çakışması yok**.
- 3306'daki `mysql` konteyneri `local-dev-stack` compose projesine ait (25 konteyner) ve `insurance-api`, `insurance-crm`, `insurance-pay`, `insurance-notify`, `car-insurance-api` projeleri de aynı yığından besleniyor. Durdurmak beş projenin yerel ortamını kırar ve migrasyona **hiçbir katkısı olmaz**.

Bu projenin MySQL'e tek bağı `.env`'deki `DB_*` anahtarlarıdır ve onlar Step 3'te PostgreSQL'e çevrildi.

```bash
docker compose up -d
docker compose ps          # healthy bekle
docker compose exec timescaledb psql -U yfin -d yfinance \
  -c "select version()" -c "select extversion from pg_extension where extname='timescaledb'"
```
Beklenen: `PostgreSQL 18.6`, `2.29.2`.

- [ ] **Step 5: `pyproject.toml` sürücüsünü değiştir**

`"pymysql>=1.1",` → `"psycopg[binary]>=3.2",`

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -c "import psycopg; print(psycopg.__version__)"
```

- [ ] **Step 6: `config.py`'yi güncelle**

`db_port: int = 3306` → `5432`, `db_user: str = "root"` → `"yfin"`.

```python
    def db_url(self, database: str | None = None) -> URL:
        """SQLAlchemy URL nesnesi; kimlik bilgileri stringe gomulmez."""
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.db_user,
            password=self.db_password,
            host=self.db_host,
            port=self.db_port,
            database=database if database is not None else self.db_name,
        )

    def bootstrap_url(self) -> URL:
        """CREATE DATABASE icin bakim veritabani baglantisi.

        PostgreSQL'de veritabani secmeden baglanilamaz. Bu URL YALNIZCA
        `CREATE DATABASE` icindir: `information_schema` VERITABANINA
        OZELDIR, yani buradan acilan bir baglanti `yfinance_test`
        icindeki semalari GOREMEZ (PG S9.1).
        """
        return self.db_url(database="postgres")
```

`query={"charset": "utf8mb4"}` satırı silinir.

- [ ] **Step 7: Kilit anahtarı için başarısız test yaz**

`tests/unit/test_lock_key.py`:

```python
"""Advisory kilit anahtari (PG S5.1). Veritabanina DOKUNMAZ."""

from __future__ import annotations

from yfin.db import SYNC_LOCK_NAME, _lock_key, _lock_key_parts


def test_key_is_deterministic() -> None:
    assert _lock_key("yfin_sync") == _lock_key("yfin_sync")
    assert _lock_key("a") != _lock_key("b")


def test_key_fits_signed_bigint() -> None:
    """pg_try_advisory_lock imzali bigint alir."""
    key = _lock_key(SYNC_LOCK_NAME)
    assert -(2**63) <= key < 2**63


def test_parts_are_valid_oids() -> None:
    """classid/objid `oid`dir: ISARETSIZ 32-bit.

    Gercek anahtar NEGATIFTIR ve classid 2^31'in USTUNDEDIR; ayristirma
    SQL'de `::int` ile yapilsaydi ERROR 22003 verirdi (PG S5.1).
    """
    key = _lock_key(SYNC_LOCK_NAME)
    assert key < 0, "bu testin anlamli olmasi icin anahtar negatif olmali"
    classid, objid = _lock_key_parts(key)
    assert 0 <= classid < 2**32
    assert 0 <= objid < 2**32
    assert classid >= 2**31, "gercek anahtarda classid 2^31 ustunde"


def test_parts_roundtrip() -> None:
    for name in ("yfin_sync", "yfin_test_lock", ""):
        key = _lock_key(name)
        classid, objid = _lock_key_parts(key)
        assert (classid << 32 | objid) == (key & 0xFFFF_FFFF_FFFF_FFFF)
```

- [ ] **Step 8: Testin başarısız olduğunu doğrula**

```bash
.venv/bin/pytest tests/unit/test_lock_key.py -v
```
Beklenen: FAIL — `ImportError: cannot import name '_lock_key' from 'yfin.db'`

- [ ] **Step 9: `db.py`'yi yeniden yaz**

```python
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
    istemcinin ayni kilidi gormesi icin gereklidir.
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
    'yfin_sync' anahtari her iki kosulu da saglar (olculdu: anahtar
    negatif, classid = 3 089 743 290), yani hata TEORIK DEGILDIR.
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
    tutan oturum.
    """
    cfg = settings or get_settings()
    # Shard'lar acik pool_size ile kurar (P4.12): gercek es zamanli
    # tuketici process basina DORTTUR -- WatermarkReader, ScopeReader ve
    # GapReader (PB S6.3, S6.5a) arti ana thread'in sembol transaction'i.
    # Invariant: process basina UST SINIR pool_size + max_overflow, yani
    # 2 x pool_size. Varsayilan max(5, workers+4) = 8 (workers=4)
    # oldugundan shard basina en fazla 16, N shard icin (N x 16) + 2.
    # PostgreSQL max_connections buna gore ayarlanir (compose: 200).
    if pool_size is None:
        pool_size = max(5, cfg.yf_max_workers + 4)

    # `options` TEK BIR DIZEDIR. Iki ayri connect_args anahtari olarak
    # verilseydi biri digerini ezerdi.
    options = ["-c timezone=UTC"]
    if schema is not None:
        # `public` ZORUNLUDUR: timescaledb eklentisi oraya kurulur ve
        # `create_hypertable` / `timescaledb_information.*` aksi halde
        # cozulemez (PG S9.1).
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
            # Kilidi kimin tuttugunu teshis edilebilir kilar (PG S5.3)
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
    # objsubid = 1 tek-bigint bicimidir; iki-int bicimi 2 kullanir.
    # Yanlis objsubid ile sorgu SESSIZCE bos doner.
    "   AND l.objsubid = 1 AND l.granted"
)


def lock_holder(conn: Any, name: str = SYNC_LOCK_NAME) -> str | None:
    """Kilidi tutan oturumun insan okunur tarifi (yoksa None).

    MySQL'de `IS_USED_LOCK` yalnizca bir connection id donduruyordu ve
    mesaj kullaniciya `SHOW PROCESSLIST` tavsiye ediyordu. PostgreSQL'de
    pid, application_name ve calisan sorgu DOGRUDAN alinir.
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

    `timeout` PARAMETRESI YOKTUR: MySQL'de tum cagiranlar 0 veriyordu ve
    PostgreSQL'de karsiligi saniye degil, ikili bir secimdir --
    `pg_advisory_lock` (sonsuz bekleme) ya da `pg_try_advisory_lock`
    (hic beklememe). Yaniltici bir parametreyi tasimak istenmedi.

    KAPSAM FARKI: PostgreSQL advisory kilitleri VERITABANI kapsamlidir;
    MySQL GET_LOCK sunucu genelindeydi. Farkli veritabanlarindaki iki
    kosu birbirini GORMEZ (PG S5.2.1).

    Kilit oturum kapsamlidir, bu yuzden ayni baglanti sonuna kadar tutulur.
    """
    key = _lock_key(name)
    conn = engine.connect()
    try:
        got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar()
        if not got:
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
```

- [ ] **Step 10: Testin geçtiğini doğrula**

```bash
.venv/bin/pytest tests/unit/test_lock_key.py -v
.venv/bin/ruff check src tests && .venv/bin/mypy --strict
```
Beklenen: 4 PASS, ruff/mypy temiz.

- [ ] **Step 11: Bağlantıyı uçtan uca doğrula**

```bash
.venv/bin/python -c "
from sqlalchemy import text
from yfin.db import create_db_engine, advisory_lock
e = create_db_engine()
with e.connect() as c:
    print(c.execute(text(\"select current_database(), current_setting('TimeZone')\")).one())
with advisory_lock(e):
    print('kilit alindi')
print('kilit birakildi')
"
```
Beklenen: `('yfinance', 'UTC')`, ardından iki satır.

- [ ] **Step 12: Commit**

```bash
git add docker-compose.yml .env.example pyproject.toml src/yfin/config.py src/yfin/db.py tests/unit/test_lock_key.py
git commit -m "feat: PostgreSQL 18 + TimescaleDB altyapisi, psycopg3, advisory lock"
```

---

### Görev 2: Tip katmanı — `base.py`, `kinds.py`, `columns.py`

**Files:**
- Modify: `src/yfin/models/base.py` (tamamı)
- Modify: `src/yfin/models/kinds.py:18-19,28-30,91-113`
- Modify: `src/yfin/models/columns.py` (tamamı)
- Modify: 15 model dosyasının **yalnızca `MYSQL_TABLE_ARGS` satırları** (Step 8 — aşağıdaki not)
- Modify: `tests/unit/test_fields.py:43` (satır bütçesi testi silinir)
- Test: `tests/unit/test_type_layer.py` (yeni)

**NEDEN `MYSQL_TABLE_ARGS` TEMİZLİĞİ BURADA, GÖREV 3'TE DEĞİL:**
`MYSQL_TABLE_ARGS` 16 dosyada geçiyor (`base.py` + 15 model dosyası).
`base.py`'den silinip model dosyaları Görev 3'e bırakılsaydı, bu görevin
doğrulama adımı `ImportError: cannot import name 'MYSQL_TABLE_ARGS'` ile
**toplama aşamasında** patlardı — tek bir test bile koşmazdı. Silme
mekaniktir (import satırı + `__table_args__` üyesi) ve tipsel kararlar
Görev 3'te kalır.

**Interfaces:**
- Consumes: —
- Produces:
  - `models.base.Base` — `metadata = MetaData(naming_convention=NAMING_CONVENTION)`
  - `models.base.NAMING_CONVENTION: dict[str, str]`
  - `TsType() -> postgresql.TIMESTAMP` (`timezone=True, precision=6`)
  - `RawJsonType() -> Text`
  - 11 collation fabrikası — hepsi `VARCHAR(n, collation="C")`
  - `PriceType/FactValueType/BigNumType` — değişmez `Numeric`
  - `symbol_fk_column(**kwargs) -> MappedColumn[str]` — imza değişmez
  - `models.columns.make_column(field: Field) -> Column[Any]` — imza DEĞİŞMEZ; `"ubig"` kind'ında adsız `CheckConstraint` ekler
  - `models.kinds.KindSpec(sql_type, convert)` — `row_cost` KALDIRILDI
  - `MYSQL_TABLE_ARGS`, `MYSQL_ROW_SIZE_LIMIT`, `estimated_row_size` KALDIRILDI

- [ ] **Step 1: Tip katmanı için başarısız invaryant testi yaz**

`tests/unit/test_type_layer.py`:

```python
"""Tip katmani invaryantlari (PG S2). Veritabanina DOKUNMAZ."""

from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP

from yfin.models import Base
from yfin.models.base import NAMING_CONVENTION, RawJsonType, TsType


def test_timestamps_are_timestamptz_with_microseconds() -> None:
    """DATETIME(6) -> TIMESTAMP(6) WITH TIME ZONE.

    Generic sqlalchemy.TIMESTAMP `precision` KABUL ETMEZ; dialect tipi
    zorunludur. Saniye hassasiyeti ticker_info_history PK'sinda
    (symbol, fetched_at) cakisma uretirdi.
    """
    ts = TsType()
    assert isinstance(ts, TIMESTAMP)
    assert ts.timezone is True
    assert ts.precision == 6


def test_raw_json_is_plain_text() -> None:
    """JSONB DEGIL: anahtar sirasini degistirir, NaN'i reddeder,
    sayilari normalize eder -- ucu de content_hash'i bozar."""
    assert isinstance(RawJsonType(), Text)


def test_metadata_has_naming_convention() -> None:
    """Adsiz kisitlarda Alembic kararsiz ad uretir ve 'bos diff'
    kapisi HIC acilmaz (PG S2.6)."""
    assert Base.metadata.naming_convention == NAMING_CONVENTION
    assert "ck" in NAMING_CONVENTION


def test_no_string_column_lacks_c_collation() -> None:
    """Her VARCHAR/String kolonu COLLATE "C" tasimali.

    kinds.py'nin urettigi duz String(n) kolonlari MySQL'de tablo
    varsayilanini aliyordu; PostgreSQL'de VERITABANI varsayilani
    (en_US.utf8) uygulanir -- yani ne "C" ne eski davranis.
    """
    offenders = [
        f"{table.name}.{col.name}"
        for table in Base.metadata.tables.values()
        for col in table.columns
        if isinstance(col.type, String)
        and col.type.length is not None
        and getattr(col.type, "collation", None) != "C"
    ]
    assert not offenders, f"collation'siz VARCHAR: {offenders}"


def test_row_size_budget_helpers_are_gone() -> None:
    """PostgreSQL'de 65535 baytlik satir siniri YOKTUR (TOAST)."""
    import yfin.models.columns as columns

    assert not hasattr(columns, "MYSQL_ROW_SIZE_LIMIT")
    assert not hasattr(columns, "estimated_row_size")


def test_mysql_table_args_is_gone() -> None:
    import yfin.models.base as base

    assert not hasattr(base, "MYSQL_TABLE_ARGS")
```

- [ ] **Step 2: Testin başarısız olduğunu doğrula**

```bash
.venv/bin/pytest tests/unit/test_type_layer.py -v
```
Beklenen: FAIL — `ImportError: cannot import name 'NAMING_CONVENTION'`

- [ ] **Step 3: `models/base.py` üst kısmını yeniden yaz**

`MYSQL_TABLE_ARGS` silinir.

```python
"""Ortak DeclarativeBase ve tip fabrikalari (PG S2)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import VARCHAR, ForeignKey, MetaData, Numeric, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, MappedColumn, mapped_column

# Kisit ADLARI deterministik olmak ZORUNDADIR: adsiz kisitlarda Alembic
# kararsiz adlar uretir ve `yfin db revision` her cagrildiginda sahte
# fark raporlar -- yani "bos diff" kapisi HIC acilmaz (PG S2.6).
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Tum tablolarin ortak tabani."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
```

- [ ] **Step 4: 11 collation fabrikasını `"C"`e çevir**

Desen (`SymbolType` örnek; kalan 10 aynı dönüşüm):

```python
def SymbolType() -> VARCHAR:  # noqa: N802
    """VARCHAR(32) COLLATE "C".

    "C" collation byte siralidir ve buyuk/kucuk harf duyarlidir; MySQL'in
    `ascii_bin`inin birebir karsiligidir. Veritabani varsayilan
    collation'i (en_US.utf8) siralamayi dile baglar ve LIKE icin indeks
    kullanilamaz hale getirirdi (olculdu: "C" kolonunda Index Scan,
    en_US.utf8 kolonunda Seq Scan).

    FK kolonlarinin collation'i ebeveynle birebir esdeger olmalidir;
    PostgreSQL MySQL gibi ERROR 3780 VERMEZ, yani sapma sessizdir ve
    invaryant testiyle korunur (test_schema_invariants.py).
    """
    return VARCHAR(32, collation="C")
```

Uzunluklar: `NewsIdType` 36, `HashType` 64, `PersonNameType` 255, `ShortHashType` 16, `BarIntervalType` 4, `RegionType` 16, `KeyTextType(length)`, `AsciiKeyType(length)`, `ProxyLabelType` 64, `HostType` 255.

`HostType` docstring'i:

```python
def HostType() -> VARCHAR:  # noqa: N802
    """Proxy hostname veya IP. VARCHAR(255) COLLATE "C".

    Hostname'ler buyuk/kucuk harf duyarsizdir (RFC 4343). MySQL bunu
    `ascii_general_ci` ile SEMADA sagliyordu; PostgreSQL'de duyarsizlik
    YAZMA YOLUNDA saglanir: host `.lower()` ile normalize edilir
    (scripts/seed_proxies.py -- PG S2.5.2). `uq_proxies_endpoint`
    boylece semantigini korur.
    """
    return VARCHAR(255, collation="C")
```

- [ ] **Step 5: `TsType` ve `RawJsonType`'i yaz**

```python
def TsType() -> TIMESTAMP:  # noqa: N802
    """Tum zaman damgalari TIMESTAMP(6) WITH TIME ZONE.

    Generic `sqlalchemy.TIMESTAMP` `precision` KABUL ETMEZ (TypeError);
    dialect tipi zorunludur.

    Alti hane SART: saniye hassasiyeti ayni saniyede PK cakismasi uretir
    (ticker_info_history PK'si (symbol, fetched_at)) ve PostgreSQL
    kesirleri YUVARLAR, kesmez (olculdu: timestamptz(0) ile .9 -> +1 sn).
    """
    return TIMESTAMP(timezone=True, precision=6)


def RawJsonType() -> Text:  # noqa: N802
    """raw_json TEXT'tir, JSON/JSONB DEGIL (PG S2.4).

    JSONB anahtar sirasini degistirir (hash yeniden hesaplanamaz), NaN
    iceren govdeyi reddeder ve sayilari normalize eder. `json` tipi de
    sozdizimi dogrular ve NaN'i reddeder. TEXT byte-for-byte sadiktir.
    """
    return Text()
```

`PRICE_PRECISION` … `BigNumType` ve `symbol_fk_column` DEĞİŞMEZ; yalnızca `FactValueType` docstring'indeki "MySQL 11. basamağı" → "PostgreSQL" (davranış aynı, ölçüldü).

- [ ] **Step 6: `models/kinds.py`'yi güncelle**

Import: `from sqlalchemy.dialects.mysql import BIGINT` → `from sqlalchemy import BigInteger`.
`_VARCHAR_COST` ve tüm `row_cost` değerleri silinir.

```python
@dataclass(frozen=True, slots=True)
class KindSpec:
    sql_type: Callable[[], TypeEngine[Any]]
    convert: Callable[[Any], Any]


def _c_string(length: int) -> Callable[[], TypeEngine[Any]]:
    """String kolonlari da COLLATE "C" tasir.

    MySQL'de tablo varsayilani (utf8mb4_0900_ai_ci) uygulaniyordu;
    PostgreSQL'de VERITABANI varsayilani uygulanirdi -- yani ne "C" ne
    eski davranis (PG S2.5).
    """
    return lambda: String(length, collation="C")


KINDS: dict[str, KindSpec] = {
    "str16": KindSpec(_c_string(16), _string_converter(16)),
    "str32": KindSpec(_c_string(32), _string_converter(32)),
    "str64": KindSpec(_c_string(64), _string_converter(64)),
    "str128": KindSpec(_c_string(128), _string_converter(128)),
    "str255": KindSpec(_c_string(255), _string_converter(255)),
    "text": KindSpec(lambda: Text(), nz.to_str),
    "dec": KindSpec(PriceType, nz.to_decimal),
    "big": KindSpec(BigNumType, _to_big),
    "int": KindSpec(lambda: Integer(), nz.to_int),
    "ubig": KindSpec(lambda: BigInteger(), _to_unsigned),
    "bool": KindSpec(lambda: Boolean(), nz.to_bool),
    "epoch_s": KindSpec(TsType, _to_epoch_seconds),
    "epoch_ms": KindSpec(TsType, _to_epoch_millis),
    "dt": KindSpec(TsType, _to_datetime),
}
```

`_to_big` ve `_to_unsigned` DEĞİŞMEZ — `_to_unsigned` (negatifi `None` yapar) CHECK'in yanında ikinci savunma hattıdır.

- [ ] **Step 7: `models/columns.py`'yi yeniden yaz**

```python
"""Field listesinden SQLAlchemy kolonu ureten fabrika (PG S2.6)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import CheckConstraint, Column

from yfin.models.fields import Field
from yfin.models.kinds import KINDS

# SATIR BUTCESI KAVRAMI YOKTUR. MySQL'in 65 535 baytlik satir siniri
# InnoDB'nin sayfa ici satir formatindan geliyordu; PostgreSQL'de boyle
# bir sinir yoktur -- genis degerler TOAST'a tasinir. Pratik sinir kolon
# sayisidir (1 600) ve en genis tablomuz onun cok altindadir.
# `MYSQL_ROW_SIZE_LIMIT` ve `estimated_row_size` bu yuzden KALDIRILDI;
# yerlerine bir sey KONMADI cunku korunacak bir sinir kalmadi.


def make_column(field: Field) -> Column[Any]:
    """Tipli kolon; hepsi NULL kabul eder (kaynak alan seti sembole gore
    degisir).

    `ubig` kind'inda CHECK eklenir: PostgreSQL'de unsigned tamsayi
    YOKTUR, `BIGINT UNSIGNED`in verdigi garanti kisitla yeniden kurulur.
    Kisit ADSIZ birakilir; adini Base.metadata'nin naming_convention'i
    uretir (`ck_{table}_{column}`) -- `Field` tablo adini tasimadigi icin
    burada uretilemezdi (PG S2.6).
    """
    spec = KINDS[field.kind]
    args: list[Any] = [field.column, spec.sql_type()]
    if field.kind == "ubig":
        args.append(CheckConstraint(f'"{field.column}" >= 0'))
    return Column(*args, nullable=True)
```

- [ ] **Step 8: 15 model dosyasından `MYSQL_TABLE_ARGS`'ı temizle**

Otorite listesi:

```bash
grep -rln "MYSQL_TABLE_ARGS" src/yfin/models | grep -v __pycache__
```
Beklenen 16 dosya (`base.py` dahil, o Step 3'te halledildi).

Her dosyada iki tür değişiklik:
1. `from yfin.models.base import (... MYSQL_TABLE_ARGS ...)` — import listesinden çıkar.
2. `__table_args__` içinden üyeyi çıkar. Kalan tek elemanlı tuple sadeleşir; **hiç eleman kalmazsa `__table_args__` satırı tamamen silinir**.

```python
# ONCE
    __table_args__ = (Index("ix_news_pub_date", "pub_date"), MYSQL_TABLE_ARGS)
# SONRA
    __table_args__ = (Index("ix_news_pub_date", "pub_date"),)

# ONCE
    __table_args__ = (MYSQL_TABLE_ARGS,)
# SONRA  -> satir tamamen silinir

# ONCE (snapshots.py:54, market.py:102, financials.py:202 -- Table() cagrilari)
    return Table(name, Base.metadata, *args, **MYSQL_TABLE_ARGS)  # type: ignore[arg-type]
# SONRA
    return Table(name, Base.metadata, *args)
```

`Table()` çağrılarındaki `# type: ignore[arg-type]` yorumu da kaldırılır — `**MYSQL_TABLE_ARGS` gittiği için gerekmez.

```bash
grep -rn "MYSQL_TABLE_ARGS" src tests | grep -v __pycache__   # BOS donmeli
```

- [ ] **Step 9: `test_fields.py`'den satır bütçesi testini sil**

`test_row_size_budget_respected` fonksiyonu ve `MYSQL_ROW_SIZE_LIMIT` / `estimated_row_size` import'ları silinir. Kalan 5 alan-adı testi DEĞİŞMEZ.

- [ ] **Step 10: Testleri çalıştır**

```bash
.venv/bin/pytest tests/unit/test_type_layer.py tests/unit/test_fields.py -v
.venv/bin/ruff check src && .venv/bin/mypy --strict
```

Beklenen: `test_type_layer.py` 6/6 PASS (Step 6'daki `_c_string` sayesinde collation testi de geçer), `test_fields.py` 5/5 PASS, ruff/mypy temiz.

Import zinciri sağlıklı: `base.py` artık `MYSQL_TABLE_ARGS` sunmuyor ve onu isteyen kimse kalmadı.

- [ ] **Step 11: Commit**

```bash
git add src/yfin/models tests/unit/test_type_layer.py tests/unit/test_fields.py
git commit -m "feat: PostgreSQL tip katmani (timestamptz, C collation, naming_convention)"
```

---

### Görev 3: Model dosyaları — 62 tablo

**Files:**
- Modify: `src/yfin/models/analysis.py`, `asof.py`, `bars.py`, `domains.py`, `financials.py`, `funds.py`, `holders.py`, `market.py`, `news.py`, `officers.py`, `prices.py`, `proxies.py`, `symbols.py`, `sync.py`, `snapshots.py` (15 dosya)
- Modify: `src/yfin/models/__init__.py:27,225` (dışa aktarım)
- Modify: `tests/conftest.py:18,60` (yalnızca iki satır — tam yeniden yapı Görev 10'da)
- Modify: `tests/unit/test_schema_invariants.py`

**Interfaces:**
- Consumes: Görev 2'nin tip fabrikaları
- Produces:
  - `Base.metadata` — 62 tablo, 14 ENUM tipi, unsigned kolon kalmamış, `price_bars.symbol` FK taşıyor
  - `models.bars.timescale_ddl() -> tuple[str, ...]` — `price_bars_partition_ddl`'in yerine; `models/__init__.py`'den dışa aktarılır. Görev 9 bunu migration'a bağlar.

**Mekanik dönüşüm tablosu (her dosyada uygulanır):**

| Bul | Değiştir |
|---|---|
| `MYSQL_TABLE_ARGS` import ve kullanımı | sil; tek elemanlıysa `__table_args__` satırını tamamen sil |
| `from sqlalchemy.dialects.mysql import BIGINT` | `from sqlalchemy import BigInteger` |
| `BIGINT(unsigned=True)` (veri kolonu) | `BigInteger` + `CheckConstraint('"<kolon>" >= 0')` |
| `BIGINT(unsigned=True), primary_key=True, autoincrement=True` | `BigInteger, Identity(always=False), primary_key=True` |
| `TINYINT(unsigned=True)` (`holding_rank`) | `SmallInteger` + `CheckConstraint('"holding_rank" BETWEEN 0 AND 255')` |
| `SMALLINT(unsigned=True)` (`proxies.port`) | `Integer` + `CheckConstraint('"port" BETWEEN 1 AND 65535')` |
| `from sqlalchemy.dialects.mysql import SMALLINT` (imzalı kullanım) | `from sqlalchemy import SmallInteger` |
| `MEDIUMTEXT()` | `Text()` |
| `VARBINARY(512)` | `LargeBinary` (PostgreSQL'de `BYTEA`) |
| `server_default="0"` / `"1"` **Boolean kolonda** | `server_default=text("false")` / `text("true")` |
| `Enum(...)` `name=` vermiyorsa | `name=` ekle |

- [ ] **Step 1: Boolean `server_default` kolonlarının otorite listesini çıkar**

```bash
.venv/bin/python -c "
from yfin.models import Base
from sqlalchemy import Boolean
for t in sorted(Base.metadata.tables.values(), key=lambda x: x.name):
    for c in t.columns:
        if isinstance(c.type, Boolean) and c.server_default is not None:
            print(f'{t.name}.{c.name}')
"
```
Beklenen 14 satır: `calendar_earnings.is_known`, `calendar_ipo.is_known`, `calendar_splits.is_known`, `domain_top_companies.is_known`, `domain_top_funds.is_known`, `domain_top_movers.is_known`, `intraday_scope.enabled`, `market_summary.is_known`, `market_summary_history.is_known`, `news_symbols.is_known`, `price_bars.is_extended`, `price_history.is_repaired`, `proxies.is_enabled`, `symbols.is_active`.

Bu liste otoritedir; hafızadan çalışılmaz.

- [ ] **Step 2: Unsigned kolonların otorite listesini çıkar**

```bash
.venv/bin/python -c "
from yfin.models import Base
for t in sorted(Base.metadata.tables.values(), key=lambda x: x.name):
    for c in t.columns:
        if getattr(c.type, 'unsigned', False):
            print(f'{t.name}.{c.name}  {c.type}  pk={c.primary_key}')
"
```
Beklenen 35 satır. 23'ü `kinds.py` `"ubig"` kind'ından gelir (Görev 2 halletti). Elle tanımlı 12: `bar_rescales.rows_affected`, `domain_metrics.employee_count`, `fund_top_holdings.holding_rank`, `price_bars.volume`, `price_history.volume`, `proxies.id`, `proxies.port`, `shares_full.shares`, `sync_run_items.id`, `sync_run_items.proxy_id`, `sync_run_items.run_id`, `sync_runs.id`.

- [ ] **Step 3: 15 model dosyasına mekanik dönüşümü uygula**

Yukarıdaki tabloyu uygula. Üç yer özel dikkat ister (Step 4-6).

- [ ] **Step 4: `models/bars.py` — FK ekle, partition DDL'ini sil**

```python
class PriceBar(Base):
    """price_history'nin intraday/cok-gunluk kardesi.

    FK TASIR: TimescaleDB hypertable'i referencing taraf olabilir
    (olculdu: ON UPDATE CASCADE + ON DELETE RESTRICT calisiyor, giden
    FK varken drop_chunks sorunsuz). MySQL 8 partition'li InnoDB'de bu
    imkansizdi (ERROR 1506) ve butunluk aylik bir oksuz-satir sorgusuyla
    korunuyordu; o sorgu artik GEREKSIZDIR (PG S7.2).
    """

    __tablename__ = "price_bars"
    __table_args__ = (
        Index("ix_price_bars_local_date", "local_date"),
        CheckConstraint('"volume" >= 0', name="ck_price_bars_volume_nonneg"),
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    bar_interval: Mapped[str] = mapped_column(BarIntervalType(), primary_key=True)
    ts_utc: Mapped[datetime] = mapped_column(TsType(), primary_key=True)
    local_date: Mapped[date] = mapped_column(nullable=False)
    open: Mapped[Decimal | None] = mapped_column(PriceType())
    high: Mapped[Decimal | None] = mapped_column(PriceType())
    low: Mapped[Decimal | None] = mapped_column(PriceType())
    close: Mapped[Decimal] = mapped_column(PriceType(), nullable=False)
    volume: Mapped[int | None] = mapped_column(BigInteger)
    is_extended: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
```

`local_date` docstring'i (yerel takvim günü, `session_date` DEĞİL) KORUNUR — veriye bağlı gerekçe.

**`price_bars_partition_ddl()` silinir ve yerine `timescale_ddl()` gelir — İKİSİ AYNI ADIMDA.** Fonksiyon `models/__init__.py:27,225` ve `tests/conftest.py:18,60` tarafından import ediliyor (ölçüldü); yalnızca silinip yerine bir şey konmasaydı bu görevin doğrulama adımı `ImportError` ile patlardı.

```python
def timescale_ddl() -> tuple[str, ...]:
    """price_bars ve price_history icin hypertable DDL'i.

    Alembic bunu autogenerate EDEMEZ; hem migration hem test conftest'i
    BU AYNI SABITI kullanir (mevcut price_bars_partition_ddl deseninin
    dogrudan yerine gecer). Aksi halde testler hypertable'siz duz
    tablolara karsi kosar ve chunk davranisi hic dogrulanmaz.

    `create_default_indexes => FALSE` ZORUNLUDUR: varsayilan davranis
    bolumleme kolonu uzerinde `price_bars_ts_utc_idx` adli bir DESC
    indeks yaratir. O indeks `public` semasinda durur, Base.metadata'da
    YOKTUR ve Alembic autogenerate onu "silinmeli" diye raporlar --
    yani `yfin db revision`in "bos diff" kapisi HIC acilmaz (PG S7.1).
    Gereken indeksler modelde acikca tanimlidir
    (ix_price_bars_local_date, ix_price_history_session_date);
    ts_utc PK'nin son bileseni oldugu icin ayri indeks gerekmez.

    INTERVAL '1 year' KULLANILMAZ: TimescaleDB ay iceren interval'i 30
    gunluk aylara cevirir ve aralik 360 gun olarak kaydolur (olculdu).
    """
    return (
        "SELECT create_hypertable('price_bars', "
        "       by_range('ts_utc', INTERVAL '7 days'), "
        "       create_default_indexes => FALSE)",
        "SELECT create_hypertable('price_history', "
        "       by_range('session_date', INTERVAL '365 days'), "
        "       create_default_indexes => FALSE)",
    )
```

Silinen gerekçe metni: tüm `MAXVALUE` / `ERROR 1493` / `ERROR 1526` / `REORGANIZE PARTITION` bloğu. TimescaleDB chunk'ları otomatik oluşturur; "aralık dışı insert" kavramı yoktur, dolayısıyla "gürültülü hata mı sessiz pruning ölümü mü" ikilemi ortadan kalkar.

**`models/__init__.py`** (satır 27 ve 225): `price_bars_partition_ddl` → `timescale_ddl`.

**`tests/conftest.py`** — bu görevde yalnızca iki satır (tam yeniden yapı Görev 10'da):

```python
# satir 18
    timescale_ddl,
# satir 60
        for stmt in timescale_ddl():
            conn.execute(text(stmt))
```

- [ ] **Step 5: `models/proxies.py` — `ON UPDATE CURRENT_TIMESTAMP`'i kaldır**

```python
    updated_at: Mapped[datetime] = mapped_column(
        TsType(),
        nullable=False,
        server_default=func.now(),
        # PostgreSQL'de `ON UPDATE CURRENT_TIMESTAMP` diye bir kolon
        # cumlecigi YOKTUR (DDL sozdizimi hatasi). Trigger yerine Python
        # tarafi secildi: tek bir kolon icin semaya gorunmez bir yan etki
        # eklemek, "davranis kodda gorunur olsun" cizgisine aykiriydi.
        # Maliyeti, ORM disindan yapilan ham UPDATE'lerin bu kolonu
        # tazelememesidir (PG S2.11).
        onupdate=lambda: datetime.now(UTC),
    )
```

`server_onupdate=FetchedValue()` ve `FetchedValue` import'u silinir.

- [ ] **Step 6: ENUM'ları adlandır ve ordinal uyarısını sil**

`models/proxies.py`:

```python
def _enum(cls: type[enum.StrEnum], name: str) -> Enum:
    """Mevcut konvansiyon: SQLAlchemy enum ISIMLERINI degil DEGERLERINI
    yazar. `name` ACIKCA verilir -- SQLAlchemy adi Python sinifindan da
    turetebilir (`proxyscheme`) ama uretilen ad snake_case
    konvansiyonuna uymaz.
    """
    return Enum(cls, values_callable=lambda e: [m.value for m in e], name=name)
```

Çağrılar: `_enum(ProxyScheme, "proxy_scheme")`, `_enum(ProxyHealth, "proxy_health")`.
`models/sync.py`'deki üç `Enum()` çağrısına `name="run_scope"`, `name="run_status"`, `name="item_status"` eklenir.

`models/financials.py` — `StatementKind` üzerindeki ordinal uyarı bloğu silinir:

```python
class StatementKind(enum.StrEnum):
    # PostgreSQL ENUM degerlerini pg_enum OID'i olarak saklar ve FK
    # ETIKET uzerinden baglanir; deger sirasi degisse bile FK bozulmaz
    # (olculdu: ALTER TYPE ... ADD VALUE ... BEFORE sonrasi bilesik FK'li
    # satir saglam kaldi). MySQL'deki ordinal tuzagi YOKTUR.
    INCOME = "income"
    BALANCE_SHEET = "balance_sheet"
    CASH_FLOW = "cash_flow"
    # get_valuation_measures cercevesi finansal tablolarla AYNI sekildedir
    # (index=kalem etiketi, kolon=donem sonu); ayri bir tablo acmak yerine
    # EAV'nin `statement` boyutuna dorduncu deger olarak girer.
    VALUATION = "valuation"
```

`STATEMENT_ENUM` / `FREQ_ENUM` paylaşımı **korunur**, gerekçesi düzeltilir:

```python
# ENUM tanimi TEK kaynaktan gelir ve iki tabloda PAYLASILIR. Gerekce
# TEK TANIM YERI ilkesidir: iki ayri Enum() nesnesinin deger listeleri
# sessizce ayrisabilir. Teknik bir cakisma riski YOKTUR -- SQLAlchemy
# ayni MetaData icinde ayni adli tipi checkfirst=False ile bile
# tekillestirir (olculdu).
```

`models/analysis.py`'deki aynı ordinal yorumu da düzeltilir.

- [ ] **Step 7: §2.5.4 ve §2.11 taramalarını yap**

```bash
# ai_ci bagimliligi: PK/UNIQUE bileseni olan string kolonlar
.venv/bin/python -c "
from yfin.models import Base
from sqlalchemy import String
for t in sorted(Base.metadata.tables.values(), key=lambda x: x.name):
    keys = {c.name for c in t.primary_key.columns}
    for c in t.constraints:
        if c.__class__.__name__ == 'UniqueConstraint':
            keys |= {x.name for x in c.columns}
    for c in t.columns:
        if c.name in keys and isinstance(c.type, String):
            print(f'{t.name}.{c.name}')
"
# proxies'e ham UPDATE yazan yer var mi (PG S2.11 riski R6)
grep -rn "UPDATE proxies\|update(Proxy)\|proxies.update" src tests scripts | grep -v __pycache__
```

Birinci taramanın bulguları spec §2.5.4 karar kuralıyla çözülür: normalize katmanında tek biçime indirgeme; `CITEXT` ve fonksiyonel UNIQUE **seçilmez**. Kuralla çözülemeyen bulgu uygulamayı durdurur ve yazara sorulur.

İkinci grep boş dönerse R6 kapanır; dönerse o çağrı `updated_at`'i açıkça set eder.

- [ ] **Step 8: İnvaryantları doğrula**

```bash
.venv/bin/pytest tests/unit/test_type_layer.py -v
.venv/bin/python -c "
from yfin.models import Base
from sqlalchemy import Enum
print('tablo:', len(Base.metadata.tables))
e = {c.type.name for t in Base.metadata.tables.values() for c in t.columns if isinstance(c.type, Enum)}
print('enum tipi:', len(e), sorted(e))
print('unsigned kalan:', [(t.name, c.name) for t in Base.metadata.tables.values()
                          for c in t.columns if getattr(c.type, 'unsigned', False)])
"
.venv/bin/ruff check src && .venv/bin/mypy --strict
```
Beklenen: `test_type_layer.py` 6/6 PASS (collation testi artık geçer), tablo **62**, enum **14** tip, unsigned listesi **boş**.

- [ ] **Step 9: `test_schema_invariants.py`'yi güncelle**

```python
from sqlalchemy.dialects.postgresql import TIMESTAMP


def test_no_timestamp_column_loses_sub_second_precision() -> None:
    """Tum damgalar TIMESTAMP(6) WITH TIME ZONE. Saniye hassasiyeti ayni
    saniyede PK cakismasi uretir VE PostgreSQL kesirleri YUVARLAR."""
    offenders = [
        f"{table.name}.{col.name}"
        for table in Base.metadata.tables.values()
        for col in table.columns
        if isinstance(col.type, TIMESTAMP)
        and (col.type.precision != 6 or col.type.timezone is not True)
    ]
    assert not offenders, offenders


def test_every_symbol_column_shares_the_symbols_collation() -> None:
    """FK kolonunun collation'i ebeveynle BIREBIR esit olmali.

    PostgreSQL MySQL gibi ERROR 3780 vermez -- sapma SESSIZDIR ve tam da
    bu yuzden test edilir. Farkli collation JOIN ve karsilastirma
    semantigini ayristirir.
    """
    parent = Base.metadata.tables["symbols"].c["symbol"].type
    for table_name, fk in _symbol_fks():
        for element in fk.elements:
            child = element.parent.type
            assert getattr(child, "collation", None) == getattr(parent, "collation", None), (
                f"{table_name}.{element.parent.name}"
            )
            assert getattr(child, "length", None) == getattr(parent, "length", None)
```

`test_every_symbol_fk_uses_the_same_policy` DEĞİŞMEZ — artık `price_bars`'ı da kapsar (Step 4).

- [ ] **Step 10: Tüm unit testleri çalıştır ve commit**

```bash
.venv/bin/pytest -q
git add src/yfin/models tests/unit/test_schema_invariants.py
git commit -m "feat: 62 tabloyu PostgreSQL tiplerine tasi (ENUM, CHECK, IDENTITY, FK)"
```

---

### Görev 4: View'lar

**Files:**
- Modify: `src/yfin/models/views.py` (tamamı)

**Interfaces:**
- Consumes: Görev 3'ün tabloları
- Produces: `V_ACTIONS_CREATE`, `V_ACTIONS_DROP`, `V_PRICE_BARS_REGULAR_CREATE`, `V_PRICE_BARS_REGULAR_DROP` — hepsi `str`

- [ ] **Step 1: `views.py`'yi yeniden yaz**

```python
"""v_actions ve v_price_bars_regular VIEW'lari (PG S8)."""

from __future__ import annotations

# CAST(... AS VARCHAR(16)): literal uzunlugu view'in kolon tipini
# belirler; sabitlenmezse yeni bir action turu eklendiginde tip sessizce
# degisir. PostgreSQL'de tirnakli literal `unknown` tipindedir ve UNION
# icinde `text`e cozulur, bu yuzden acik cast korunur.
#
# CHAR(16) KULLANILMAZ: PostgreSQL'de `bpchar`tir ve sonda BOSLUK
# DOLDURUR. VARCHAR dogru karsiliktir.
#
# security_invoker = true (PG 15+): view cagiranin yetkisiyle okur --
# MySQL'deki `SQL SECURITY INVOKER` ile ayni niyet.
V_ACTIONS_CREATE = """
CREATE OR REPLACE VIEW v_actions
  WITH (security_invoker = true) AS
  SELECT symbol, ex_date    AS action_date,
         CAST('DIVIDEND'     AS VARCHAR(16)) AS action_type,
         amount AS action_value FROM dividends
  UNION ALL
  SELECT symbol, split_date, CAST('SPLIT'        AS VARCHAR(16)), ratio  FROM splits
  UNION ALL
  SELECT symbol, gain_date,  CAST('CAPITAL_GAIN' AS VARCHAR(16)), amount FROM capital_gains
"""

V_ACTIONS_DROP = "DROP VIEW IF EXISTS v_actions"

# Yalniz normal seans barlari. Amaci kolaylik degil KAZA ONLEMEDIR:
# is_extended filtresini unutmak, seans disi dusuk hacimli barlari normal
# seansa karistirir ve hesaplanan her gostergeyi sessizce bozar.
# Varsayilan okuma yolu view olmalidir (PB S5.10).
#
# price_bars bir hypertable'dir; duz view uzerinde chunk exclusion
# calisir (olculdu: Custom Scan (ChunkAppend) + chunk seviyesinde
# Index Cond).
V_PRICE_BARS_REGULAR_CREATE = """
CREATE OR REPLACE VIEW v_price_bars_regular
  WITH (security_invoker = true) AS
  SELECT symbol, bar_interval, ts_utc, local_date,
         open, high, low, close, volume
    FROM price_bars
   WHERE is_extended = false
"""

V_PRICE_BARS_REGULAR_DROP = "DROP VIEW IF EXISTS v_price_bars_regular"
```

- [ ] **Step 2: Doğrula ve commit**

```bash
.venv/bin/ruff check src && .venv/bin/mypy --strict
git add src/yfin/models/views.py
git commit -m "feat: view'lari PostgreSQL sozdizimine tasi (security_invoker, VARCHAR cast)"
```

---

### Görev 5: `persistence.py` → `PostgresRowWriter`

**Files:**
- Modify: `src/yfin/persistence.py` (tamamı)
- Modify: `MySQLRowWriter` geçen 15 dosya daha (Step 5'te grep ile bulunur)
- Test: `tests/unit/test_insert_chunk.py`

**Interfaces:**
- Consumes: `TableWrite` (`datasets/base.py`) — değişmez
- Produces:
  - `persistence.PostgresRowWriter(session: Session)` — `MySQLRowWriter`'ın yerine
  - `persistence.dedupe_rows(rows: list[dict[str, Any]], key_columns: tuple[str, ...], monotonic_columns: tuple[str, ...]) -> list[dict[str, Any]]`
  - `align_rows`, `apply_write`, `INSERT_CHUNK`, `VERIFY_CHUNK` — değişmez
  - Protokoller değişmez

- [ ] **Step 1: Dedupe için başarısız test yaz**

`tests/unit/test_insert_chunk.py`'ye ekle:

```python
from yfin.persistence import dedupe_rows


class TestDedupeRows:
    """PostgreSQL ON CONFLICT DO UPDATE ayni komutta ayni satira IKI KEZ
    dokunamaz (21000 cardinality_violation). MySQL bunu sorunsuz
    yutuyordu, bu yuzden dataset'lerde garanti YOKTUR (PG S4.1.1)."""

    def test_last_wins_for_repeated_key(self) -> None:
        rows = [
            {"symbol": "AAPL", "session_date": "2026-01-02", "close": 1},
            {"symbol": "AAPL", "session_date": "2026-01-02", "close": 2},
            {"symbol": "MSFT", "session_date": "2026-01-02", "close": 9},
        ]
        out = dedupe_rows(rows, ("symbol", "session_date"), ())
        assert out == [
            {"symbol": "AAPL", "session_date": "2026-01-02", "close": 2},
            {"symbol": "MSFT", "session_date": "2026-01-02", "close": 9},
        ]

    def test_preserves_first_seen_order(self) -> None:
        rows = [{"k": "b", "v": 1}, {"k": "a", "v": 1}, {"k": "b", "v": 2}]
        out = dedupe_rows(rows, ("k",), ())
        assert [r["k"] for r in out] == ["b", "a"]

    def test_monotonic_column_takes_group_max(self) -> None:
        """GREATEST yalnizca MEVCUT DB satiriyla yeni satiri
        karsilastirir, ayni batch'teki iki satiri DEGIL. Duz
        'son kazanir' monotonikligi dilim icinde geri yazardi."""
        rows = [
            {"symbol": "AAPL", "session_date": "2026-01-02", "is_repaired": True},
            {"symbol": "AAPL", "session_date": "2026-01-02", "is_repaired": False},
        ]
        out = dedupe_rows(rows, ("symbol", "session_date"), ("is_repaired",))
        assert len(out) == 1
        assert out[0]["is_repaired"] is True

    def test_none_never_beats_a_value_in_monotonic_column(self) -> None:
        rows = [{"k": "a", "m": 5}, {"k": "a", "m": None}]
        out = dedupe_rows(rows, ("k",), ("m",))
        assert out[0]["m"] == 5

    def test_untouched_when_keys_are_unique(self) -> None:
        rows = [{"k": "a"}, {"k": "b"}]
        assert dedupe_rows(rows, ("k",), ()) is rows
```

- [ ] **Step 2: Testin başarısız olduğunu doğrula**

```bash
.venv/bin/pytest tests/unit/test_insert_chunk.py -k Dedupe -v
```
Beklenen: FAIL — `ImportError: cannot import name 'dedupe_rows'`

- [ ] **Step 3: `dedupe_rows`'u yaz**

`persistence.py` başlığı ve import:

```python
"""PostgreSQL yazma mekanigi (PG S4).

Bu modul, dataset sozlesmesinden (datasets/base.py) AYRIDIR: sozlesme
hangi verinin nereye yazilacagini tanimlar, buradaki kod bunu
PostgreSQL'e nasil yazacagini bilir. Dataset'ler `RowWriter` protokolune
bagimlidir, SQLAlchemy'ye degil.
"""

from sqlalchemy.dialects.postgresql import insert as pg_insert
```

```python
def dedupe_rows(
    rows: list[dict[str, Any]],
    key_columns: tuple[str, ...],
    monotonic_columns: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Ayni anahtardan yalnizca bir satir birakir (son kazanir).

    ZORUNLUDUR: PostgreSQL `ON CONFLICT DO UPDATE` ayni komutta ayni
    satira IKI KEZ dokunamaz (ERROR 21000, "cannot affect row a second
    time"). MySQL `ON DUPLICATE KEY UPDATE` bunu sorunsuz yutuyordu, bu
    yuzden dataset'lerin cogunda dilim ici tekillik garantisi YOKTUR.

    `monotonic_columns` ISTISNADIR: grup icindeki EN BUYUK deger alinir.
    `GREATEST` yalnizca mevcut DB satiriyla yeni satiri karsilastirir,
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
```

- [ ] **Step 4: `PostgresRowWriter`'ı yaz**

```python
class PostgresRowWriter:
    """RowWriter'in PostgreSQL uygulamasi."""

    def write(self, write: TableWrite) -> int:
        table = self._table(write.table)

        # Silme, rows bos olsa da yapilir: "kapsam bosaldi" durumunda
        # erken cikilsaydi eski satirlar kalici olarak kalirdi (S6.2).
        if write.mode == "replace_scope":
            self._delete_scope(table, write)

        if not write.rows:
            return 0

        # align_rows TUM listeye, DILIMLEMEDEN ONCE uygulanir. Sonra
        # uygulansaydi her dilim farkli bir kolon setiyle ve farkli bir
        # guncelleme haritasiyla yazilirdi.
        rows = align_rows(write.rows)
        # dedupe de dilimlemeden ONCE: tekrarli iki anahtar farkli
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

        `index_elements` KUME OLARAK TAM ESLESMELIDIR: alt kume de ust
        kume de "there is no unique or exclusion constraint matching"
        hatasi verir (sira onemsizdir). `key_columns`in gercek bir
        PK/UNIQUE'e karsilik geldigi test_persistence_contract.py'de
        invaryant olarak korunur.
        """
        stmt = pg_insert(table).values(rows)
        update_map: dict[str, Any] = {}
        for col in write.update_columns:
            if col not in present:
                continue
            if col in write.monotonic_columns:
                # Kaynak ayni satir icin bir kez 1, ertesi kez 0
                # bildirebilir (repair heuristikleri pencere uzunluguna
                # baglidir); GREATEST bilgiyi geri yazmaz.
                # PostgreSQL GREATEST NULL'i YOK SAYAR (MySQL NULL
                # dondururdu) -- burada daha guvenlidir: kaynak bir kez
                # NULL bildirse bile mevcut deger korunur.
                update_map[col] = func.greatest(table.c[col], stmt.excluded[col])
            else:
                update_map[col] = stmt.excluded[col]
        if update_map:
            return stmt.on_conflict_do_update(
                index_elements=list(write.key_columns), set_=update_map
            )
        # Hicbir kolon guncellenmiyorsa satir sadece eklenir. MySQL'de
        # `ON DUPLICATE KEY UPDATE` bos olamadigi icin `first_key =
        # first_key` hilesi gerekiyordu; PostgreSQL'de DO NOTHING var.
        return stmt.on_conflict_do_nothing(index_elements=list(write.key_columns))
```

`_verify()` gövdesi DEĞİŞMEZ, docstring'i:

```python
        """Anahtar varligi sorgusu (S8.6).

        Etkilenen satir sayisi dogrulama icin KULLANILMAZ: `ON CONFLICT
        DO NOTHING` cakisma nedeniyle atlanan satiri SAYMAZ (olculdu:
        INSERT 0 0). Anahtar varligi sorgusu daha guclu bir garanti
        verir -- "kac satir dokunuldu"yu degil, "istenen anahtarlarin
        kaci GERCEKTEN tabloda" sorusunu cevaplar.
        """
```

`INSERT_CHUNK` yorumundan `max_allowed_packet` cümlesi, `VERIFY_CHUNK` yorumundan `range_optimizer_max_mem_size` cümlesi çıkarılır; kalan gerekçeler korunur.

- [ ] **Step 5: 15 dosyadaki referansı güncelle**

Otorite listesi (hafızadan çalışılmaz):

```bash
grep -rl "MySQLRowWriter" src tests | grep -v __pycache__ | sort
```
Beklenen 16 dosya (`persistence.py` dahil).

```bash
grep -rl "MySQLRowWriter" src tests | grep -v __pycache__ \
  | xargs sed -i '' 's/MySQLRowWriter/PostgresRowWriter/g'
grep -rn "MySQLRowWriter" src tests | grep -v __pycache__   # BOS donmeli
```

- [ ] **Step 6: Doğrula ve commit**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check src tests && .venv/bin/mypy --strict
git add src/yfin tests
git commit -m "feat: PostgresRowWriter (ON CONFLICT) + dilim ici anahtar dedupe"
```

---

### Görev 6: `runner.py` SQLSTATE, `normalize.py` tz-aware, `rescale.py` ham SQL

**Files:**
- Modify: `src/yfin/normalize.py:162,172,177,214,321`
- Modify: `src/yfin/runner.py:612-632`
- Modify: `src/yfin/rescale.py:216-253`
- Modify: `src/yfin/cli.py:63`
- Modify: `now(UTC).replace(tzinfo=None)` geçen 12 dosya
- Modify: `tests/unit/test_bars_normalize.py`, `test_financials.py`, `test_market.py`, `test_normalize.py`

**Interfaces:**
- Consumes: —
- Produces: `normalize.to_datetime_utc` / `epoch_to_datetime` artık **UTC-aware** `datetime` döndürür

- [ ] **Step 1: `normalize.py`'yi tz-aware yap**

`to_datetime_utc` ve `epoch_to_datetime` içindeki `.replace(tzinfo=None)` kaldırılır:

```python
    """... Donen deger UTC-AWARE'dir.

    MySQL DATETIME(6) tz tasimadigi icin damga naive'e indiriliyordu;
    PostgreSQL kolonu `timestamptz`tir ve tz bilgisini SAKLAR (PG S2.3).
    """
```

`:321` civarındaki `allow_nan` yorumu:

```python
    # allow_nan=False ZORUNLUDUR. Gerekce motor degisimiyle DEGISMEDI,
    # yalnizca belirtisi degisti: NaN iceren bir govde `content_hash`
    # uzerinden karsilastirildiginda `NaN != NaN` oldugu icin hash kapisi
    # HER KOSUDA acilir ve degismeyen veri surekli yeniden yazilir.
```

- [ ] **Step 2: 12 dosyadaki `now(UTC).replace(tzinfo=None)` desenini düzelt**

```bash
grep -rn "now(UTC).replace(tzinfo=None)" src tests scripts | grep -v __pycache__
```
Beklenen 17 satır, 12 dosya: `cli.py`, `cli_bars.py`, `datasets/bars.py`, `domain_runner.py`, `market_runner.py`, `proxy/repository.py`, `rescale.py`, `runner.py`, `shard.py`, `tests/live/test_live_analysis.py`, `tests/live/test_live_bars.py`, `tests/repo/test_proxy_repo.py`.

```bash
grep -rl "now(UTC).replace(tzinfo=None)" src tests scripts | grep -v __pycache__ \
  | xargs sed -i '' 's/now(UTC)\.replace(tzinfo=None)/now(UTC)/g'
```

- [ ] **Step 3: Kalan `.replace(tzinfo=None)` çağrılarını tek tek incele**

```bash
grep -rn "replace(tzinfo=None)" src tests scripts | grep -v __pycache__
```

Kalan ~24 satırın her biri **okunur**: tz-aware bir damgayı naive'e indiren çağrılar kaldırılır; `date`/`datetime` kurgulayan veya karşılaştırma için normalize eden çağrılar korunur. Bu adım mekanik değildir.

Atlanan bir dosya SESSİZ kalır: psycopg3 naive datetime'ı bağlantı TZ'sine (UTC) göre yorumlar, sonuç doğru çıkar ama tip tutarsızlığı kalıcılaşır. Bu yüzden grep çıktısı otoritedir.

- [ ] **Step 4: `cli.py:_parse_date`'i UTC-aware yap**

```python
def _parse_date(value: str) -> datetime:
    """YYYY-MM-DD -> UTC-aware datetime (kolonlar timestamptz)."""
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
```

- [ ] **Step 5: 4 test dosyasındaki `tzinfo is None` iddialarını çevir**

```bash
grep -rn "tzinfo is None" tests | grep -v __pycache__
```
Beklenen 4 dosya: `test_bars_normalize.py`, `test_financials.py`, `test_market.py`, `test_normalize.py`.

Her `assert X.tzinfo is None` → `assert X.tzinfo is UTC`; yorumlar da güncellenir (*"MySQL DATETIME tz tasimaz"* → *"kolon timestamptz; normalize UTC-aware dondurur"*).

- [ ] **Step 6: `runner.py` retry'yi SQLSTATE tabanlı yap**

```python
# PostgreSQL SQLSTATE'leri. Semboller shard'lara dagitildigi icin iki
# process ayni news / news_symbols satirina yazabilir; tek process'te bu
# risk yoktu.
#   40001 serialization_failure
#   40P01 deadlock_detected
# 55P03 (lock_not_available) LISTEDE YOKTUR: bu kod yolunda hic olusmaz
# cunku NOWAIT / SKIP LOCKED kullanilmiyor. Gerekcesiz bir SQLSTATE'i
# yeniden denemek, ileride NOWAIT eklenirse yanlis davranisi sessizce
# mesrulastirirdi.
_RETRYABLE_SQLSTATES = frozenset({"40001", "40P01"})


def _is_lock_conflict(exc: BaseException) -> bool:
    """Hata METNI degil SQLSTATE'e bakilir: yerellestirilmis mesajlardan
    ve surucu bicim degisikliklerinden etkilenmez."""
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    return sqlstate in _RETRYABLE_SQLSTATES
```

`_persist_with_retry` docstring'inden `innodb_rollback_on_timeout` paragrafı silinir:

```python
    """Sembol transaction'i; kilit catismasinda jitter'li yeniden deneme.

    Transaction sembol kapsamli (S8.7) ve idempotent (S7.2) oldugu icin
    yeniden calistirmak guvenlidir. PostgreSQL'de hata alan transaction
    HER ZAMAN abort durumuna gecer ve ROLLBACK disinda komut kabul etmez,
    bu yuzden yeniden denemeden once rollback ZORUNLUDUR.
    """
```

- [ ] **Step 7: `rescale.py` ham SQL'ini çevir**

```python
    claim = session.execute(
        text(
            "INSERT INTO bar_rescales (symbol, split_date, ratio, applied_at, rows_affected) "
            "VALUES (:symbol, :split_date, :ratio, :now, 0) "
            "ON CONFLICT (symbol, split_date) DO NOTHING"
        ),
        {"symbol": symbol, "split_date": split_day, "ratio": ratio, "now": now},
    )
    if not _rowcount(claim):
        return 0  # baska bir oturum almis
```

Docstring:

```python
    """Tek split: once SLOTU AL, sonra UPDATE et.

    Slot `INSERT ... ON CONFLICT DO NOTHING` ile alinir, kilitle DEGIL.
    `SELECT ... FOR UPDATE` CALISMAZ: var olmayan bir satir uzerindeki
    FOR UPDATE kilit almaz, iki oturum da "satir yok, uygulayacagim" der
    ve cakisma INSERT aninda patlar.

    rowcount 1 ise slot bizimdir; 0 ise baska bir oturum onceden almistir
    ve UPDATE calistirilmaz.
    """
```

`FLOOR` yorumu:

```python
            # FLOOR SART: 3:2 split'te volume*1.5 kesirli cikar. FLOOR
            # olmadan `numeric` bir deger `bigint` kolona atanirken
            # YUVARLANIR; FLOOR ile kesme davranisi ACIKTIR.
            # Bu UPDATE artik bir HYPERTABLE'a gider; `ts_utc < :boundary`
            # kosulu sayesinde yalnizca ilgili chunk'lara dokunur
            # (PG S7.3 -- compression bu yuzden acilmadi).
```

- [ ] **Step 8: Doğrula ve commit**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check src tests && .venv/bin/mypy --strict
git add src/yfin tests
git commit -m "feat: tz-aware normalize, SQLSTATE retry, rescale ON CONFLICT"
```

---

### Görev 7: Collation normalizasyonları

**Files:**
- Modify: `src/yfin/datasets/symbols.py:58-59`
- Modify: `src/yfin/cli.py:111-160` (`_filtered_symbols`)
- Modify: `scripts/seed_proxies.py:43,78,90`
- Modify: `src/yfin/datasets/holders/insider_transactions.py:127-129` (YALNIZCA yorum)
- Test: `tests/unit/test_collation_normalization.py` (yeni)

**Interfaces:**
- Consumes: —
- Produces: `symbols.exchange` / `.quote_type` yazarken `.upper()`; `proxies.host` yazarken `.lower()`

- [ ] **Step 1: Gerçek fonksiyon adlarını oku**

```bash
sed -n '40,80p' src/yfin/datasets/symbols.py
sed -n '30,100p' scripts/seed_proxies.py
sed -n '45,70p' src/yfin/proxy/dsn.py
sed -n '120,135p' src/yfin/datasets/holders/insider_transactions.py
```

Step 2'deki test **gerçek API'ye** yazılır; API teste uydurulmaz. Aşağıdaki isimler (`normalize_symbol_fields`, `parse_line`, `parse_dsn`) yer tutucudur ve bu adımda gerçekleriyle değiştirilir.

- [ ] **Step 2: Başarısız regresyon testlerini yaz**

`tests/unit/test_collation_normalization.py`:

```python
"""MySQL'in utf8mb4_0900_ai_ci varsayilaninin kaldirilmasiyla ortaya
cikan davranis farklari (PG S2.5). Veritabanina DOKUNMAZ."""

from __future__ import annotations


class TestSymbolFieldsAreUppercased:
    """`--exchange nms` ile `--exchange NMS` ayni sonucu vermeli.

    MySQL'de kolonlar ai_ci oldugu icin karsilastirma zaten duyarsizdi.
    PostgreSQL'de kolonlar COLLATE "C"dir; duyarsizlik YAZMA ve SORGU
    yollarinin ikisinde de `.upper()` ile saglanir.
    """

    def test_normalize_upper_cases_exchange_and_quote_type(self) -> None:
        from yfin.datasets.symbols import normalize_symbol_fields

        row = normalize_symbol_fields({"exchange": "nms", "quote_type": "equity"})
        assert row["exchange"] == "NMS"
        assert row["quote_type"] == "EQUITY"

    def test_normalize_tolerates_missing_and_none(self) -> None:
        from yfin.datasets.symbols import normalize_symbol_fields

        assert normalize_symbol_fields({})["exchange"] is None
        assert normalize_symbol_fields({"exchange": None})["exchange"] is None


class TestProxyHostIsLowercased:
    """Hostname'ler buyuk/kucuk harf duyarsizdir (RFC 4343).

    MySQL bunu `ascii_general_ci` ile SEMADA sagliyordu ve
    `uq_proxies_endpoint` buna dayaniyordu. PostgreSQL'de duyarsizlik
    yazma yolunda saglanir; aksi halde ayni proxy iki kez eklenirdi.
    """

    def test_seed_line_lowercases_host(self) -> None:
        from scripts.seed_proxies import parse_line

        left = parse_line("eu-1,http,HOST.Example.COM,8080,user,pass")
        right = parse_line("eu-2,http,host.example.com,8080,user,pass")
        assert left.host == "host.example.com"
        assert left.host == right.host

    def test_dsn_path_already_lowercases(self) -> None:
        """urlsplit hostname'i zaten kucuk harfe indirir; burada yapacak
        is yoktur ama regresyon olarak sabitlenir."""
        from yfin.proxy.dsn import parse_dsn

        dsn = "http://u:pw" + "@" + "HOST.Example.COM:8080"
        assert parse_dsn(dsn).host == "host.example.com"
```

- [ ] **Step 3: Testin başarısız olduğunu doğrula**

```bash
.venv/bin/pytest tests/unit/test_collation_normalization.py -v
```
Beklenen: FAIL — `'nms' != 'NMS'` ve host küçültülmemiş.

- [ ] **Step 4: `datasets/symbols.py`'de `.upper()` uygula**

```python
        # .upper() SART: kolonlar artik COLLATE "C"dir (buyuk/kucuk harf
        # duyarli). MySQL'de ai_ci sayesinde `--exchange nms` calisiyordu;
        # duyarsizlik simdi yazma ve sorgu yollarinda saglanir
        # (PG S2.5.1). Yahoo bu alanlari zaten buyuk harfle donduruyor --
        # yani bu veride kimliktir -- ama `yfin symbols add` ile elle
        # eklenen sembolde tek normalizasyon yeri burasidir.
```

- [ ] **Step 5: `cli.py:_filtered_symbols`'da filtre girdisini normalize et**

```python
    """--exchange / --quote-type / --suffix filtreleri; AND'lenir (AH S6.4).

    Girdi `.upper()` ile normalize edilir cunku kolonlar COLLATE "C"dir
    ve yazma yolu da buyuk harfe cevirir (PG S2.5.1). `func.upper`
    KULLANILMAZ: kolonu fonksiyonla sarmalamak `ix_symbols_exchange`
    indeksini kullanilamaz hale getirirdi.
    """
    stmt = base_stmt
    if exchanges:
        stmt = stmt.where(Symbol.exchange.in_([e.strip().upper() for e in exchanges]))
    if quote_types:
        stmt = stmt.where(Symbol.quote_type.in_([q.strip().upper() for q in quote_types]))
```

- [ ] **Step 6: `scripts/seed_proxies.py`'de `.lower()` uygula**

```python
    # RFC 4343: hostname'ler buyuk/kucuk harf duyarsizdir. MySQL bunu
    # ascii_general_ci ile SEMADA sagliyordu; PostgreSQL'de COLLATE "C"
    # kullanildigi icin normalizasyon BURADA yapilir, yoksa ayni proxy
    # farkli harf bicimleriyle iki kez eklenir ve uq_proxies_endpoint
    # semantigini kaybeder (PG S2.5.2).
    host = parts[2].strip().lower()
```

(Gerçek alan indeksi Step 1'de okunan koda göre ayarlanır.)

- [ ] **Step 7: `insider_transactions.py` yorumunu düzelt — DAVRANIS DEGISMEZ**

```python
            # Hash PYTHON tarafinda HAM dizeden hesaplanir, yani
            # 'Sale' ve 'sale' AYRI iki satirdir. Bu BILINCLIDIR ve motor
            # degisiminden ETKILENMEZ: PK bileseni `fact_hash`tir ve
            # karsilastirma zaten Python tarafinda yapiliyordu
            # (PG S2.5.3). Normalizasyon EKLENMEZ -- eklenseydi bugun
            # ayri sayilan iki olay tek satira inerdi.
```

- [ ] **Step 8: Doğrula ve commit**

```bash
.venv/bin/pytest tests/unit/test_collation_normalization.py -v
.venv/bin/pytest -q
.venv/bin/ruff check src tests scripts && .venv/bin/mypy --strict
git add src/yfin scripts tests
git commit -m "feat: collation normalizasyonlari (exchange upper, host lower)"
```

---

### Görev 8: `maintenance.py` silme + `cli_bars.py` temizliği

**Files:**
- Delete: `src/yfin/maintenance.py`
- Modify: `src/yfin/cli_bars.py:17,175-196,212`

**Interfaces:**
- Consumes: —
- Produces: `yfin bars maintain` — partition adımı ve öksüz satır adımı YOK

- [ ] **Step 1: `maintenance.py`'yi sil**

```bash
git rm src/yfin/maintenance.py
grep -rn "maintenance" src tests scripts | grep -v __pycache__
```

Modülün tamamı MySQL partition bakımıydı: `existing_partitions()` (`information_schema.partitions` + `DATABASE()`), `missing_partitions()` (`LOOKAHEAD_MONTHS=12`, `p2026_09` adlandırması), `add_partitions()` (`ALTER TABLE ... ADD PARTITION`). Üçünün de PostgreSQL'de karşılığı yoktur — TimescaleDB chunk'ları **otomatik** oluşturur.

- [ ] **Step 2: `cli_bars.py`'den partition adımını çıkar**

`from yfin.maintenance import add_partitions, missing_partitions` import'u ve partition ekleme bloğu (`:175-183`) silinir.

```python
@bars_app.command("maintain")
def bars_maintain() -> None:
    """price_bars bakim isleri.

    PARTITION EKLEME ADIMI YOKTUR: price_bars bir TimescaleDB
    hypertable'idir ve chunk'lari otomatik olusturur. MySQL'de aylik
    partition'lari elle eklemek gerekiyordu ve bakim atlanirsa insert
    ERROR 1526 ile duserdi (PG S3.2).
    """
```

- [ ] **Step 3: Öksüz satır raporunu kaldır**

`:187-196` arasındaki öksüz satır sorgusu ve raporu silinir. `price_bars` artık `symbols`'a FK taşıyor (Görev 3), yani öksüz satır **oluşamaz**; sorgu ölü koddur.

- [ ] **Step 4: `SUM(resolved_at IS NULL)`'ı düzelt**

```python
        rows = session.execute(
            text(
                # MySQL'de `SUM(x IS NULL)` boolean'i ortuk olarak int'e
                # ceviriyordu. PostgreSQL'de SUM(boolean) YOKTUR (42883);
                # FILTER dogru ve daha okunakli karsiliktir.
                "SELECT reason, COUNT(*) AS total, "
                "       COUNT(*) FILTER (WHERE resolved_at IS NULL) AS open_gaps "
                "  FROM bar_gaps GROUP BY reason"
            )
        ).all()
```

Raporlama döngüsü yeni kolon adlarına göre güncellenir.

- [ ] **Step 5: Doğrula ve commit**

```bash
.venv/bin/ruff check src && .venv/bin/mypy --strict
.venv/bin/pytest -q
.venv/bin/yfin bars maintain --help
git add -A src/yfin
git commit -m "refactor: MySQL partition bakimini kaldir (hypertable otomatik)"
```

---

### Görev 9: Alembic — sıfırdan initial migration + TimescaleDB DDL

**Files:**
- Delete: `migrations/versions/*.py` (10 revizyon)
- Create: `migrations/versions/<yeni>_initial_postgres_timescaledb.py`
- Modify: `migrations/env.py:19-36`
- Modify: `src/yfin/cli.py:194-210` (`db_create`)

**Interfaces:**
- Consumes: Görev 2-4'ün `Base.metadata`'sı, Görev 3'ün `timescale_ddl()`'i, Görev 1'in `bootstrap_url()`'ü
- Produces: tek initial revizyon; `yfin db create` PostgreSQL semantiğiyle çalışır

- [ ] **Step 1: `timescale_ddl()`'in hazır olduğunu doğrula**

`timescale_ddl()` Görev 3'te yazıldı (import zincirini kırmamak için). Bu görev onu yalnızca migration'a bağlar.

```bash
.venv/bin/python -c "
from yfin.models import timescale_ddl
for s in timescale_ddl(): print(s)
"
```
Beklenen: iki `create_hypertable` ifadesi, ikisinde de `create_default_indexes => FALSE`.

- [ ] **Step 2: `cli.py:db_create`'i yeniden yaz**

```python
@db_app.command("create")
def db_create() -> None:
    """Veritabanlarini ve timescaledb eklentisini olusturur (yoksa)."""
    from sqlalchemy import create_engine

    settings = get_settings()
    # CREATE DATABASE transaction icinde CALISMAZ -> AUTOCOMMIT sart
    engine = create_engine(settings.bootstrap_url(), isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        for name in (settings.db_name, settings.db_test_name):
            # `CREATE DATABASE IF NOT EXISTS` PostgreSQL'de YOKTUR
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine.dispose()

    # Eklenti HER veritabaninda AYRI kurulur (veritabani duzeyindedir)
    for name in (settings.db_name, settings.db_test_name):
        db_engine = create_engine(settings.db_url(name), isolation_level="AUTOCOMMIT")
        with db_engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
        db_engine.dispose()

    typer.echo(f"veritabani hazir: {settings.db_name}, {settings.db_test_name}")
```

- [ ] **Step 3: `migrations/env.py`'yi güncelle**

`EXPRESSION_INDEXES` ve `include_object` **korunur** (ifade tabanlı indeksleri Alembic PostgreSQL'de de geri okuyamaz). Yorumdan MySQL referansı temizlenir. **`_timescaledb` filtresi EKLENMEZ:**

```python
# `include_schemas` VARSAYILAN (False) BIRAKILIR: Alembic yalnizca
# search_path'in ilk semasina bakar, bu yuzden TimescaleDB'nin ic
# semalarindaki (_timescaledb_internal) chunk tablolari autogenerate
# ciktisinda HIC GORUNMEZ (olculdu). Bir `_timescaledb` filtresi
# gereksizdir ve asil problemi (varsayilan hypertable indeksi,
# PG S7.1) gizlerdi.
```

- [ ] **Step 4: Eski revizyonları sil ve veritabanını sıfırla**

```bash
git rm migrations/versions/*.py
touch migrations/versions/.gitkeep
docker compose down -v && docker compose up -d
docker compose ps      # healthy bekle
.venv/bin/yfin db create
```

- [ ] **Step 5: Initial migration'ı üret ve elle tamamla**

```bash
.venv/bin/alembic revision --autogenerate -m "initial postgres timescaledb"
```

Üretilen dosyada `upgrade()` sonuna (tablolar ve indeksler oluşturulduktan SONRA):

```python
def upgrade() -> None:
    # ... autogenerate ciktisi (ENUM tipleri, tablolar, indeksler) ...

    # Hypertable'lar: Alembic bunlari autogenerate EDEMEZ (PG S7.6)
    for stmt in timescale_ddl():
        op.execute(stmt)

    op.execute(V_ACTIONS_CREATE)
    op.execute(V_PRICE_BARS_REGULAR_CREATE)


def downgrade() -> None:
    op.execute(V_PRICE_BARS_REGULAR_DROP)
    op.execute(V_ACTIONS_DROP)
    # Hypertable'lar DROP TABLE ile birlikte gider; ayrica bir islem
    # gerekmez.
    # `DROP EXTENSION timescaledb` YAPILMAZ: eklenti veritabani
    # duzeyindedir ve `yfin db create`in urunudur. Dusurulurse
    # upgrade/downgrade dongusu ikinci turda patlar (PG S12).
    # ... autogenerate ciktisinin ters sirasi ...
```

`CREATE EXTENSION` migration'a **konmaz** (`db create` kurar); konsaydı `downgrade` simetrisi bozulurdu.

- [ ] **Step 6: Upgrade / boş diff / downgrade döngüsünü doğrula**

```bash
.venv/bin/yfin db upgrade head
.venv/bin/alembic revision --autogenerate -m probe
sed -n '/def upgrade/,/^def downgrade/p' migrations/versions/*probe*.py
```
Beklenen: `upgrade()` gövdesi **boş** (yalnızca `pass` veya yorum). Boş değilse fark giderilir ve tekrar denenir.

```bash
rm migrations/versions/*probe*.py
.venv/bin/yfin db downgrade base
.venv/bin/yfin db upgrade head
```
Beklenen: üçü de hatasız.

- [ ] **Step 7: Hypertable'ları doğrula**

```bash
docker compose exec timescaledb psql -U yfin -d yfinance -c "
select hypertable_name, column_name, column_type, time_interval
  from timescaledb_information.dimensions;" -c "
select tablename, indexname from pg_indexes
 where tablename in ('price_bars','price_history') order by 1,2;"
```
Beklenen: iki hypertable (`ts_utc` / 7 days, `session_date` / 360-365 days); indeks listesinde `price_bars_ts_utc_idx` ve `price_history_session_date_idx` **BULUNMAMALI**.

- [ ] **Step 8: Commit**

```bash
git add -A migrations src/yfin/models/bars.py src/yfin/models/__init__.py src/yfin/cli.py
git commit -m "feat: sifirdan PostgreSQL initial migration + TimescaleDB hypertable"
```

---

### Görev 10: Test altyapısı — şema izolasyonu

**Files:**
- Modify: `tests/conftest.py` (tamamı)
- Modify: `tests/helpers.py:20-63`

**Interfaces:**
- Consumes: `create_db_engine(..., schema=...)` (Görev 1), `timescale_ddl()` (Görev 9), `lock_holder()` (Görev 1)
- Produces: `bootstrap_engine`, `test_db_engine`, `test_schema`, `test_engine`, `db_session`, `committed_session`, `cleanup_tables` fixture'ları

- [ ] **Step 1: `helpers.py`'yi şema tabanlı yap**

```python
def schema_name(base: str) -> str:
    """Test SEMASI SURECE ozeldir: `<base>_<pid>`.

    Sabit tek sema kullanildiginda iki pytest kosusu birbirinin
    tablolarini dusuruyordu. Sema adini surece baglamak bu sinifi
    imkansiz kilar.

    PostgreSQL'de SEMA kullanilir, VERITABANI degil: `CREATE DATABASE`
    transaction disinda calismak zorundadir, sablon veritabanini kopyalar
    ve pahalidir; `CREATE SCHEMA` siradan bir DDL'dir (PG S9.1).
    """
    return f"{base}_{os.getpid()}"


def drop_stale_schemas(engine: Engine, base: str) -> list[str]:
    """Kesilen kosulardan kalan `<base>_<pid>` semalarini dusurur.

    `engine` TEST VERITABANINA bagli olmalidir, bootstrap (`postgres`)
    baglantisina DEGIL: `information_schema.schemata` VERITABANINA
    OZELDIR ve bootstrap baglantisi test veritabanindaki semalari
    GOREMEZ -- temizlik sessizce hicbir sey yapar ve semalar sonsuza
    kadar birikirdi (PG S9.1).

    YALNIZCA PID'i artik yasamayan semalar silinir, boylece ES ZAMANLI
    bir kosunun semasina dokunulmaz.
    """
    prefix = f"{base}_"
    dropped: list[str] = []
    with engine.connect() as conn:
        names = list(
            conn.execute(
                text("SELECT schema_name FROM information_schema.schemata")
            ).scalars()
        )
        for name in names:
            suffix = name[len(prefix) :] if name.startswith(prefix) else ""
            if not suffix.isdigit() or pid_is_alive(int(suffix)):
                continue
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
            conn.commit()
            dropped.append(name)
    return dropped
```

`pid_is_alive` DEĞİŞMEZ.

- [ ] **Step 2: `conftest.py`'yi iki engine'e ayır**

```python
@pytest.fixture(scope="session")
def bootstrap_engine(settings: Settings) -> Iterator[Engine]:
    """`postgres` bakim veritabani. YALNIZCA CREATE DATABASE icin.

    Sema islemleri BU ENGINE ILE YAPILAMAZ: information_schema
    veritabanina ozeldir (PG S9.1).
    """
    engine = create_engine(settings.bootstrap_url(), isolation_level="AUTOCOMMIT")
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def test_db_engine(settings: Settings, bootstrap_engine: Engine) -> Iterator[Engine]:
    """Test VERITABANINA bagli engine (sema secmeden).

    Sema olusturma/silme ve bayat sema temizligi bunu kullanir.
    """
    try:
        with bootstrap_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"),
                {"n": settings.db_test_name},
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{settings.db_test_name}"'))
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"PostgreSQL erisilemiyor: {exc}")

    engine = create_engine(
        settings.db_url(settings.db_test_name), isolation_level="AUTOCOMMIT"
    )
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def test_engine(
    settings: Settings, test_schema: str, test_db_engine: Engine
) -> Iterator[Engine]:
    """Surece ozel test semasi; kosu sonunda TAMAMEN dusurulur."""
    drop_stale_schemas(test_db_engine, settings.db_test_name)
    with test_db_engine.connect() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{test_schema}" CASCADE'))
        conn.execute(text(f'CREATE SCHEMA "{test_schema}"'))

    # search_path'te `public` ZORUNLUDUR: timescaledb eklentisi oraya
    # kurulur ve `create_hypertable` aksi halde cozulemez
    # (ERROR: function by_range(unknown, interval) does not exist).
    engine = create_db_engine(
        settings,
        settings.db_test_name,
        schema=test_schema,
        application_name=f"yfin-pytest-{os.getpid()}",
    )
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        conn.execute(text(V_ACTIONS_CREATE))
        conn.execute(text(V_PRICE_BARS_REGULAR_CREATE))
        # create_all() hypertable'lari BILMEZ (Alembic de autogenerate
        # edemez). Migration ile AYNI sabit burada da uygulanmazsa
        # price_bars duz bir tablo olarak olusur ve chunk davranisi hic
        # dogrulanamaz (PG S7.6).
        for stmt in timescale_ddl():
            conn.execute(text(stmt))
        conn.commit()
    yield engine
    engine.dispose()

    # DROP SCHEMA ... CASCADE chunk'lari da temizler (olculdu:
    # "drop cascades to table _timescaledb_internal._hyper_1_1_chunk",
    # sonrasinda chunks_left = 0).
    with test_db_engine.connect() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{test_schema}" CASCADE'))
```

`db_session` ve `committed_session` DEĞİŞMEZ. `cleanup_tables`'daki backtick'ler:

```python
                conn.execute(text(f'DELETE FROM "{name}"'))
```

- [ ] **Step 3: `_guard_concurrent_live_runs`'ı çevir**

```python
@pytest.fixture(scope="session", autouse=True)
def _guard_concurrent_live_runs(request: pytest.FixtureRequest) -> None:
    """Ikinci bir live kosusu baslatildiysa ANLASILIR sekilde durdurur.

    CANLI veritabanina baglanir, test veritabanina DEGIL: PostgreSQL
    advisory kilitleri VERITABANI KAPSAMLIDIR (MySQL GET_LOCK sunucu
    genelindeydi). `run_sync` kilidi canli veritabaninda alir, bu yuzden
    guard da orada bakmalidir; test veritabanina bakilsaydi kilit HIC
    gorunmez ve guard sessizce islevsiz kalirdi (PG S5.2.1).

    Guard yalnizca GERCEKTEN live testi kosulacaksa calisir. Karar
    `-m` ifadesinin METNINDEN degil, TOPLANAN testlerden verilir.
    """
    if not any(item.get_closest_marker("live") for item in request.session.items):
        return
    from sqlalchemy import create_engine

    from yfin.config import get_settings
    from yfin.db import SYNC_LOCK_NAME, lock_holder

    engine = create_engine(get_settings().db_url())
    try:
        with engine.connect() as conn:
            holder = lock_holder(conn, SYNC_LOCK_NAME)
    except Exception:  # noqa: BLE001 - sunucu yoksa asil fixture zaten skip eder
        return
    finally:
        engine.dispose()

    if holder is not None:
        pytest.exit(
            f"'{SYNC_LOCK_NAME}' advisory kilidi MESGUL ({holder}). "
            "Baska bir sync ya da live test kosusu devam ediyor; live testler "
            "es zamanli KOSTURULAMAZ. Once o kosunun bitmesini bekleyin.",
            returncode=1,
        )
```

- [ ] **Step 4: Şemanın kurulduğunu ve düştüğünü doğrula**

```bash
.venv/bin/pytest -m repo --collect-only -q | tail -3
.venv/bin/pytest -m repo -x -q 2>&1 | tail -20
```
İlk koşuda birçok test hâlâ FAIL edecek (ham SQL Görev 11'de) — **beklenen budur**. Kritik olan: şema açılıyor, `create_all` + `timescale_ddl()` hatasız çalışıyor.

```bash
docker compose exec timescaledb psql -U yfin -d yfinance_test -c "
select count(*) as kalan_sema from information_schema.schemata
 where schema_name like 'yfinance_test_%';"
```
Beklenen: koşu bittikten sonra `0`.

- [ ] **Step 5: Commit**

```bash
git add tests/conftest.py tests/helpers.py
git commit -m "feat: surec basina sema izolasyonu (iki engine, search_path, hypertable)"
```

---

### Görev 11: Mekanik test uyarlamaları

**Files:**
- Modify: `tests/repo/test_advisory_lock.py:24,28,55`
- Modify: `tests/repo/test_bars_schema.py:44,63`
- Modify: `tests/repo/test_domain_audit.py:69`
- Modify: `tests/repo/test_repository.py:295`
- Modify: `tests/live/test_live_sync.py:168`
- Modify: `tests/repo/test_financials_repo.py:286,626-628,656,664`
- Modify: `tests/repo/test_domain_schema.py:132,144`
- Modify: `tests/repo/test_analysis_schema.py:128`
- Modify: `tests/repo/test_domain_writes.py:161`
- Modify: backtick taramasının bulduğu tüm dosyalar

**Interfaces:**
- Consumes: Görev 10'un fixture'ları
- Produces: `pytest -m repo` yeşil

- [ ] **Step 1: Backtick taraması — otorite listesi**

```bash
grep -rn '`' tests src scripts --include='*.py' | grep -v __pycache__
```

Ham SQL içindeki her backtick'li tanımlayıcı çift tırnağa çevrilir (PostgreSQL'de backtick geçersizdir). Docstring içindeki backtick'ler dokunulmaz.

- [ ] **Step 2: `test_advisory_lock.py`'yi çevir**

```python
from yfin.db import advisory_lock, lock_holder


def test_lock_is_free_when_not_held(test_engine: Engine) -> None:
    with test_engine.connect() as conn:
        assert lock_holder(conn, "yfin_test_lock") is None


def test_holder_is_reported_while_held(test_engine: Engine) -> None:
    """MySQL IS_USED_LOCK yalnizca connection id veriyordu; artik pid ve
    application_name dogrudan gorunur (PG S5.3)."""
    with advisory_lock(test_engine, "yfin_test_lock"):
        with test_engine.connect() as conn:
            holder = lock_holder(conn, "yfin_test_lock")
        assert holder is not None
        assert "pid=" in holder
    with test_engine.connect() as conn:
        assert lock_holder(conn, "yfin_test_lock") is None
```

- [ ] **Step 3: `test_bars_schema.py`'yi hypertable testine çevir**

```python
def test_price_bars_is_a_hypertable(db_session: Session) -> None:
    """MySQL'de aylik RANGE COLUMNS partition'lari elle olusturuluyordu;
    TimescaleDB chunk'lari OTOMATIK yaratir. Test AMACI degismedi:
    bolumleme gercekten kurulu mu (PG S7.1)."""
    row = db_session.execute(
        text(
            "SELECT column_name, column_type, time_interval "
            "  FROM timescaledb_information.dimensions "
            " WHERE hypertable_name = 'price_bars'"
        )
    ).one()
    assert row.column_name == "ts_utc"
    assert "time zone" in row.column_type
    assert row.time_interval == timedelta(days=7)


def test_price_history_is_a_hypertable(db_session: Session) -> None:
    row = db_session.execute(
        text(
            "SELECT column_name, time_interval "
            "  FROM timescaledb_information.dimensions "
            " WHERE hypertable_name = 'price_history'"
        )
    ).one()
    assert row.column_name == "session_date"


def test_price_bars_creates_chunks_on_insert(
    committed_session: Session, cleanup_tables: list[str]
) -> None:
    """Chunk'lar yazma aninda olusur; "aralik disi insert" diye bir sey
    YOKTUR (MySQL'de ERROR 1526 verirdi)."""
    cleanup_tables.append("price_bars")
    # iki FARKLI aya ait iki bar yaz (sembol bootstrap'i cagiran tarafta)
    ...
    chunks = committed_session.execute(
        text(
            "SELECT count(*) FROM timescaledb_information.chunks "
            " WHERE hypertable_name = 'price_bars'"
        )
    ).scalar_one()
    assert chunks >= 2


def test_price_bars_has_no_default_partition_index(db_session: Session) -> None:
    """create_default_indexes => FALSE olmadan Alembic 'bos diff'
    kapisi HIC acilmaz (PG S7.1)."""
    names = set(
        db_session.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = 'price_bars'")
        ).scalars()
    )
    assert "price_bars_ts_utc_idx" not in names
```

- [ ] **Step 4: MySQL fonksiyonlarını çevir**

| Dosya:satır | Eski | Yeni |
|---|---|---|
| `test_domain_audit.py:69` | `SELECT LAST_INSERT_ID()` | `INSERT ... RETURNING id` |
| `test_repository.py:295` | `SHA2(raw_json, 256) = content_hash` | `encode(sha256(raw_json::bytea), 'hex') = content_hash` |
| `test_live_sync.py:168` | `SHA2(raw_json,256) <> content_hash` | `encode(sha256(raw_json::bytea), 'hex') <> content_hash` |

- [ ] **Step 5: `information_schema` / ENUM sorgularını çevir**

`COLUMN_TYPE` PostgreSQL'de yoktur; ENUM etiketleri `pg_enum`dan okunur. Ortak yardımcıyı `tests/helpers.py`'ye ekle:

```python
def enum_labels(session: Session, type_name: str) -> list[str]:
    """PostgreSQL'de ENUM etiketleri `pg_enum`dan okunur.

    MySQL `information_schema.COLUMNS.COLUMN_TYPE` icinde
    "enum('a','b')" dizesi donduruyordu; PostgreSQL'de boyle bir kolon
    YOKTUR.
    """
    return list(
        session.execute(
            text(
                "SELECT e.enumlabel FROM pg_enum e "
                "  JOIN pg_type t ON t.oid = e.enumtypid "
                " WHERE t.typname = :n ORDER BY e.enumsortorder"
            ),
            {"n": type_name},
        ).scalars()
    )
```

`test_financials_repo.py:286,626-628` ve `test_domain_schema.py:132,144` bunu kullanır.

`DATABASE()` → şema bağlamında `current_schema()`, veritabanı bağlamında `current_database()`.

`test_financials_repo.py:656,664`: `CREATE DATABASE IF NOT EXISTS \`{stale}\`` → `CREATE SCHEMA IF NOT EXISTS "{stale}"`; `SCHEMA_NAME` → `schema_name`; ilgili `DROP DATABASE` → `DROP SCHEMA ... CASCADE`.

`test_domain_writes.py:161`: `SELECT COLUMN_NAME FROM information_schema.COLUMNS` → `SELECT column_name FROM information_schema.columns` (sonuç kolonu artık küçük harf).

`test_analysis_schema.py:128`: backtick'li kolon listesi üreten yardımcı çift tırnağa çevrilir.

- [ ] **Step 6: `-m repo` yeşil olana kadar koş**

```bash
.venv/bin/pytest -m repo -q
```
Her başarısızlıkta hata mesajındaki SQLSTATE ve ifade **okunur**; tahminle düzeltme yapılmaz. Beklenen: tümü PASS.

- [ ] **Step 7: Commit**

```bash
git add tests
git commit -m "test: repo testlerini PostgreSQL/TimescaleDB'ye uyarla"
```

---

### Görev 12: Yeni invaryant ve regresyon testleri

**Files:**
- Modify: `tests/unit/test_persistence_contract.py`
- Modify: `tests/repo/test_repository.py`
- Modify: `tests/repo/test_proxy_repo.py`

**Interfaces:**
- Consumes: Görev 5, 9, 10
- Produces: —

- [ ] **Step 1: `registry.py` API'sini oku**

```bash
sed -n '1,80p' src/yfin/datasets/registry.py
grep -n "def \|produces" src/yfin/datasets/base.py | head -40
```

`key_columns` değerleri `normalize()` sırasında üretiliyorsa, invaryant testi mevcut fixture'ları (`tests/fixtures/`) kullanarak her dataset'i `normalize` edip dönen `TableWrite`'ları denetler. Step 2'deki iskelet gerçek API'ye göre hizalanır.

- [ ] **Step 2: `key_columns` ↔ PK/UNIQUE invaryant testini yaz**

`tests/unit/test_persistence_contract.py`'ye ekle:

```python
def _valid_key_sets(table_name: str) -> list[set[str]]:
    from yfin.models import Base

    table = Base.metadata.tables[table_name]
    out = [{c.name for c in table.primary_key.columns}]
    for constraint in table.constraints:
        if constraint.__class__.__name__ == "UniqueConstraint":
            out.append({c.name for c in constraint.columns})
    for index in table.indexes:
        if index.unique:
            out.append({c.name for c in index.columns})
    return out


def test_every_declared_key_matches_a_real_unique_constraint() -> None:
    """`ON CONFLICT (cols)` KUME OLARAK TAM ESLESME ister: alt kume de
    ust kume de "there is no unique or exclusion constraint matching"
    hatasi verir (olculdu). Bu invaryant olmadan yanlis `key_columns`
    ile eklenen bir dataset ancak URETIMDE patlar (PG S9.2).

    Statik AST taramasi yildiz-acilimini ve degiskenden gelen tuple'i
    cozemez (spec Ek A); bu yuzden kontrol CALISMA ZAMANINDA yapilir.
    """
    from yfin.models import Base

    problems: list[str] = []
    checked = 0
    skipped: list[str] = []

    for name, writes in _collect_table_writes():   # Step 1'de hizalanir
        if not writes:
            skipped.append(name)
            continue
        for write in writes:
            checked += 1
            if write.table not in Base.metadata.tables:
                problems.append(f"{name}: bilinmeyen tablo {write.table}")
                continue
            if set(write.key_columns) not in _valid_key_sets(write.table):
                problems.append(
                    f"{name}: {write.table} {write.key_columns} "
                    "hicbir PK/UNIQUE ile eslesmiyor"
                )

    assert not problems, problems
    # Sessiz kapsam kaybi olmasin: fixture'i olmayan dataset'ler
    # LISTELENIR, gorunmez sekilde atlanmaz.
    assert checked > 0, "hicbir TableWrite denetlenmedi"
    print(f"denetlenen TableWrite: {checked}, fixture'siz dataset: {skipped}")
```

- [ ] **Step 3: Testin geçtiğini doğrula**

```bash
.venv/bin/pytest tests/unit/test_persistence_contract.py -v -s
```
Beklenen: PASS; çıktıda denetlenen `TableWrite` sayısı ve atlanan dataset listesi görünür.

- [ ] **Step 4: `GREATEST` davranışını gerçek motorda sabitle**

`tests/repo/test_repository.py`'ye:

```python
@pytest.mark.repo
def test_greatest_ignores_null_and_orders_booleans(db_session: Session) -> None:
    """PostgreSQL GREATEST NULL'i YOK SAYAR (MySQL NULL dondururdu) ve
    boolean uzerinde calisir (false < true). Monotonik kolon
    (price_history.is_repaired) bu iki davranisa dayanir (PG S4.2)."""
    row = db_session.execute(
        text(
            "SELECT greatest(true, NULL::boolean) AS a, "
            "       greatest(false, true) AS b, "
            "       greatest(5, NULL::int) AS c"
        )
    ).one()
    assert row.a is True
    assert row.b is True
    assert row.c == 5


@pytest.mark.repo
def test_monotonic_column_never_regresses(db_session: Session) -> None:
    """Ayni (symbol, session_date) icin once is_repaired=True, sonra
    False yazilirsa deger True KALMALIDIR."""
    writer = PostgresRowWriter(db_session)
    # ... sembol bootstrap + iki ardisik write ...
    # son okumada is_repaired True olmali


@pytest.mark.repo
def test_repeated_key_in_one_write_does_not_raise(db_session: Session) -> None:
    """Dedupe olmadan ERROR 21000 cardinality_violation alinirdi
    (PG S4.1.1)."""
    writer = PostgresRowWriter(db_session)
    # ... ayni anahtardan iki satir iceren TEK bir TableWrite yaz ...
    # hata olmamali, son deger kazanmali
```

- [ ] **Step 5: Collation regresyonuna gerçek veritabanı ayağı ekle**

`tests/repo/test_proxy_repo.py`'ye:

```python
@pytest.mark.repo
def test_duplicate_host_case_violates_unique(
    committed_session: Session, cleanup_tables: list[str]
) -> None:
    """HOST.example.com ve host.example.com AYNI proxy'dir (RFC 4343).
    Normalizasyon olmadan ikisi de eklenir ve uq_proxies_endpoint
    semantigini kaybederdi (PG S2.5.2)."""
    cleanup_tables.append("proxies")
    # ... normalize eden yazma yolundan iki kez ekle ...
    # ikincisi IntegrityError vermeli
```

- [ ] **Step 6: Tüm testleri çalıştır ve commit**

```bash
.venv/bin/pytest -q && .venv/bin/pytest -q -m repo
git add tests
git commit -m "test: key_columns invaryanti, GREATEST davranisi, dedupe ve collation regresyonlari"
```

---

### Görev 13: Yorum temizliği, paket meta ve nihai denetim

**Files:**
- Modify: `pyproject.toml:4,41-44`
- Modify: `src/yfin/__init__.py:1`, `src/yfin/cli.py:45,369`
- Modify: `src/yfin/models/holders.py`, `base.py`, `funds.py`, `market.py`, `domains.py`
- Modify: `src/yfin/datasets/common.py`
- Modify: kalan tüm dosyalardaki MySQL yorumları

**Interfaces:**
- Consumes: —
- Produces: sıfır MySQL referansı

- [ ] **Step 1: Paket metasını güncelle**

`pyproject.toml`:
```toml
description = "yfinance Ticker API -> PostgreSQL 18 + TimescaleDB veri hatti"
```
```toml
markers = [
    "repo: gercek PostgreSQL/TimescaleDB gerektirir (ag yok)",
    "live: gercek Yahoo API + gercek PostgreSQL gerektirir",
]
```

`src/yfin/__init__.py:1`: `"""yfinance -> PostgreSQL veri hatti."""`
`src/yfin/cli.py:45`: `typer.Typer(help="yfinance -> PostgreSQL veri hatti", ...)`
`src/yfin/cli.py:369`: `"""Veri cekip PostgreSQL'e yazar. ..."""`

- [ ] **Step 2: Kategori (a) — motora bağlı yorumları sil/yeniden yaz**

```bash
grep -rniE 'mysql|innodb|utf8mb4|error 1[0-9]{3}|error 3[0-9]{3}|sql_mode|client_found_rows|max_allowed_packet|range_optimizer' \
  src tests scripts --include='*.py' | grep -v __pycache__
```

Her satır spec §13(a) uyarınca silinir veya PostgreSQL karşılığıyla yeniden yazılır.

- [ ] **Step 3: Kategori (c) — sınırda olan yorumları yeniden yaz**

`models/base.py:BarIntervalType`:

```python
def BarIntervalType() -> VARCHAR:  # noqa: N802
    """Bar interval kodu: '1m', '5m', '15m', '60m', '1wk', '1mo'.

    ENUM DEGILDIR. Gerekce PostgreSQL'de motordan DEGIL, tek dogruluk
    kaynagi ilkesinden gelir: gecerli deger kumesi Python tarafinda
    `BAR_INTERVALS`tedir (models/bars.py) ve semada tekrarlanmasi iki
    kaynagin sessizce ayrismasi demektir. (MySQL'de ek bir gerekce
    vardi -- ENUM'a deger eklemek 464 milyon satirli tabloda ALTER TABLE
    demekti; PostgreSQL'de `ALTER TYPE ... ADD VALUE` ucuzdur, yani o
    gerekce dustu.)

    KOLON ADI `bar_interval`, `interval` DEGIL: INTERVAL PostgreSQL'de
    de bir TIP ADIDIR ve tirnaksiz ham SQL'de bozar.

    COLLATE "C": PK bilesenidir; buyuk/kucuk harf duyarsiz bir collation
    '1M' = '1m' sayardi -- sessiz satir kaybi.
    """
    return VARCHAR(4, collation="C")
```

`models/holders.py`: `(sinir 3072)` → `(PostgreSQL btree tuple siniri 2704)`. Ölçülen 548 ve 1055 bayt değerleri KORUNUR.

`models/base.py:FactValueType` ve `datasets/common.py`: *"MySQL 11. basamağı SESSİZCE yuvarlar"* → *"PostgreSQL NUMERIC(38,10) 11. basamagi SESSIZCE yuvarlar (olculdu: numeric(5,2) <- 1.239 -> 1.24, uyari yok)"*. **Karar (Python tarafında `quantize`) korunur.**

`models/funds.py:169` (`rank`) ve `market.py:163` (`last_value`): PostgreSQL'de de pencere fonksiyonudur; karar korunur, ifade genelleştirilir.

`models/domains.py:132-145`: `ERROR 3823` (CHECK + referential action) bloğu **silinir**. `parent_key` FK'sinin `ON UPDATE RESTRICT` kalması bilinçlidir ve gerekçesi sadeleşir:

```python
    # ON UPDATE RESTRICT: `domain_key` Yahoo'nun sabit slug'idir
    # ('technology', 'software-infrastructure') ve yeniden adlandirilmasi
    # beklenmez. Beklenmedik bir sekilde denenirse RESTRICT GORUNUR bir
    # hata verir, sessiz bir bozulma degil. ON DELETE RESTRICT ise bir
    # sektoru silmenin 145 endustriyi oksuz birakmasini engeller.
```

- [ ] **Step 4: Kategori (b) — veriye bağlı yorumların korunduğunu doğrula**

```bash
grep -rn "olculen\|olculdu\|medyan" src --include='*.py' | grep -v __pycache__ | wc -l
git show HEAD~12:src/yfin/models/domains.py > /dev/null 2>&1 && echo "taban erisilebilir"
```

Bu sayı Görev 1 öncesindeki değerle **karşılaştırılır**. Belirgin bir düşüş varsa veriye bağlı bir ölçüm yanlışlıkla silinmiştir:

```bash
git show $(git rev-list --max-parents=0 HEAD):src/yfin/models/domains.py \
  | grep -c "olculen\|olculdu"
```

- [ ] **Step 5: Paketi yeniden kur (egg-info tazelenir)**

```bash
.venv/bin/pip install -e ".[dev]"
grep -rn "mysql\|pymysql" src/yfin.egg-info/ 2>/dev/null
```
Beklenen: boş. `egg-info` eski `Summary` ve `Requires-Dist: pymysql` satırlarını taşıyorsa Step 6 taraması asla boşalmaz.

- [ ] **Step 6: Nihai denetim taraması**

```bash
grep -rniE 'mysql|innodb|utf8|charset|ai_ci|_bin|pymysql|on_duplicate|get_lock|release_lock|is_used_lock|is_free_lock|longtext|mediumtext|tinyint|varbinary|sql_mode|auto_increment|max_allowed_packet|client_found_rows|last_insert_id|sha2\(|column_type|database\(\)|information_schema\.partitions' \
  --exclude-dir='*.egg-info' --exclude-dir='__pycache__' \
  src tests migrations scripts pyproject.toml alembic.ini
```
Beklenen: **boş**. Her kalan satır tek tek okunur; körlemesine dışlama eklenmez.

- [ ] **Step 7: Uçtan uca sıfırdan kurulum**

```bash
docker compose down -v && docker compose up -d
docker compose ps      # healthy bekle
.venv/bin/yfin db create
.venv/bin/yfin db upgrade head
.venv/bin/yfin db downgrade base
.venv/bin/yfin db upgrade head
```
Beklenen: dördü de hatasız.

- [ ] **Step 8: Bitiş ölçütlerinin tamamını koş**

```bash
.venv/bin/ruff check src tests scripts
.venv/bin/mypy --strict
.venv/bin/pytest -q
.venv/bin/pytest -q -m repo
.venv/bin/alembic revision --autogenerate -m probe
sed -n '/def upgrade/,/^def downgrade/p' migrations/versions/*probe*.py
rm migrations/versions/*probe*.py
```
Beklenen: ilk dördü temiz/yeşil; `probe` migration'ın `upgrade()` gövdesi boş.

```bash
docker compose exec timescaledb psql -U yfin -d yfinance -c "
select hypertable_name, count(*) as chunks
  from timescaledb_information.chunks group by 1;"
```
Beklenen: veri yazıldıysa her hypertable için 0 < chunks < 100 (spec §7.1 kabul ölçütü).

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "chore: MySQL referanslarini temizle, paket metasini guncelle"
```

---

## Kapsam dışı (bilinçli)

Spec'te açıkça ertelendi; plan da uygulamaz:

- **Compression** (spec §7.3) — `rescale.py` geriye dönük UPDATE yaptığı için ölçüm ister.
- **Retention policy** (§7.4) — `bar_gaps` kaydını uygulama yazar; otomatik `drop_chunks` "piyasa kapalıydı mı, biz mi kaçırdık" ayrımını yok eder.
- **`prune.py` değişikliği** (§7.4) — saf SQLAlchemy Core, motor değişiminden etkilenmiyor.
- **Continuous aggregate** (§7.5).
- **Hash boyutu** (`add_dimension`, §7.1).
- **`pytest -m live`** — bitiş ölçütü değil; ağ ve Yahoo'ya bağlı. İstenirse ayrıca koşulur.
