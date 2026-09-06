"""Ortak test fixture'lari."""

from __future__ import annotations

import os

# MODUL SEVIYESINDE ve HER `yfin` import'undan ONCE (CFG S8.3).
#
# Autouse bir session fixture'i YETMEZ: pytest once tum test modullerini
# IMPORT eder, fixture'lar ondan SONRA calisir -- import aninda kurulan
# bir `Settings` yukleyiciyi coktan tetiklemis olurdu. Ayrica
# session-scoped bir fixture function-scoped `monkeypatch`i isteyemez
# (ScopeMismatch).
#
# Zorunludur cunku `load_overrides` `db_name`e -- yani URETIM SEMASINA --
# baglanir, oysa testler surece ozel bir semada kosar
# (`tests/helpers.py`). `setdefault` kullanilir: DB yolunu sinayan repo
# testleri `monkeypatch.delenv` + `config.reset_settings()` ile bu
# korumayi bilincli olarak kaldirir.
#
# Spawn edilen child'lar `os.environ`i miras alir (shard.py), dolayisiyla
# degisken onlara da gecer.
os.environ.setdefault("YF_SETTINGS_SOURCE", "env")

from collections.abc import Iterator  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import Engine, create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from helpers import drop_stale_schemas, schema_name  # noqa: E402
from yfin.config import Settings, get_settings  # noqa: E402
from yfin.db import create_db_engine  # noqa: E402
from yfin.models import (  # noqa: E402
    V_ACTIONS_CREATE,
    V_PRICE_BARS_REGULAR_CREATE,
    Base,
    timescale_ddl,
)


@pytest.fixture(scope="session")
def settings() -> Settings:
    return get_settings()


@pytest.fixture(scope="session")
def test_schema(settings: Settings) -> str:
    """Bu pytest surecine ait sema adi."""
    return schema_name(settings.db_test_name)


@pytest.fixture(scope="session")
def bootstrap_engine(settings: Settings) -> Iterator[Engine]:
    """`postgres` bakim veritabani. YALNIZCA `CREATE DATABASE` icin.

    Sema islemleri BU ENGINE ILE YAPILAMAZ: `information_schema`
    VERITABANINA OZELDIR, yani buradan acilan bir baglanti
    `yfinance_test` icindeki semalari GOREMEZ (PG S9.1).

    AUTOCOMMIT sart: `CREATE DATABASE` transaction blogunda calismaz.
    """
    engine = create_engine(settings.bootstrap_url(), isolation_level="AUTOCOMMIT")
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def test_db_engine(settings: Settings, bootstrap_engine: Engine) -> Iterator[Engine]:
    """Test VERITABANINA bagli engine (sema secmeden).

    Sema olusturma/silme ve bayat sema temizligi bunu kullanir.
    Eklenti burada kurulur cunku `CREATE EXTENSION` VERITABANI
    duzeyindedir; atlanirsa `create_hypertable` "function
    by_range(unknown, interval) does not exist" ile duser.
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
    """Surece ozel test SEMASI; kosu sonunda TAMAMEN dusurulur."""
    drop_stale_schemas(test_db_engine, settings.db_test_name)
    with test_db_engine.connect() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{test_schema}" CASCADE'))
        conn.execute(text(f'CREATE SCHEMA "{test_schema}"'))

    # search_path'te `public` ZORUNLUDUR: timescaledb eklentisi oraya
    # kurulur ve `create_hypertable` / `timescaledb_information.*` aksi
    # halde cozulemez. Unutulursa testler SESSIZCE duz tabloya duser
    # (PG S9.1).
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
        # dogrulanamaz (PB S9.2, PG S7.6).
        for stmt in timescale_ddl():
            conn.execute(text(stmt))
        conn.commit()
    yield engine
    engine.dispose()

    # `DROP SCHEMA ... CASCADE` chunk'lari da temizler (olculdu:
    # "drop cascades to table _timescaledb_internal._hyper_1_1_chunk").
    with test_db_engine.connect() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{test_schema}" CASCADE'))


@pytest.fixture
def db_session(test_engine: Engine) -> Iterator[Session]:
    """Her test kendi transaction'inda; sonunda rollback (S9.2)."""
    connection = test_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def committed_session(test_engine: Engine) -> Iterator[Session]:
    """GERCEKTEN COMMIT EDEN oturum; es zamanlilik testleri icindir.

    `db_session` tek baglantida acilip sonunda rollback edilen bir
    transaction'dir: ikinci bir oturum onun yazdigini GOREMEZ ve deadlock
    hic olusmaz. Bu fixture kendi satirlarini kendisi temizler.
    """
    session = Session(bind=test_engine, expire_on_commit=False)
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def cleanup_tables(test_engine: Engine) -> Iterator[list[str]]:
    """Testin kirlettigi tablolari sonunda bosaltir (FK sirasi cagiranda)."""
    tables: list[str] = []
    try:
        yield tables
    finally:
        with test_engine.connect() as conn:
            for name in tables:
                conn.execute(text(f'DELETE FROM "{name}"'))
            conn.commit()


@pytest.fixture(scope="session", autouse=True)
def _guard_concurrent_live_runs(request: pytest.FixtureRequest) -> None:
    """Ikinci bir live kosusu baslatildiysa ANLASILIR sekilde durdurur.

    `run_sync` 'yfin_sync' advisory kilidini alir; iki live kosusu ust
    uste binerse ikincisi LockNotAcquired ile duser ve bu, dosya
    fixture'larinda ERROR, testlerde tutarsiz satir sayisi olarak
    gorunur - yani KOD HATASI gibi. Bu oturumda tam olarak bu yasandi:
    arka planda suren bir live kosusunun uzerine ikincisi baslatildi ve
    5 failed + 18 error uretti; hicbiri gercek bir regresyon degildi.

    Guard yalnizca GERCEKTEN live testi kosulacaksa calisir. Karar
    `-m` ifadesinin METNINDEN degil, TOPLANAN testlerden verilir:
    `"live" in markexpr` kontrolu `-m "not live"` ifadesinde de DOGRU
    doner ve unit kosusunu bosuna durdururdu.
    """
    if not any(item.get_closest_marker("live") for item in request.session.items):
        return
    from sqlalchemy import create_engine

    from yfin.config import get_settings
    from yfin.db import SYNC_LOCK_NAME, lock_holder

    # CANLI veritabanina baglanir, test veritabanina DEGIL. PostgreSQL
    # advisory kilitleri VERITABANI KAPSAMLIDIR (MySQL GET_LOCK sunucu
    # genelindeydi): `run_sync` kilidi canli veritabaninda alir, bu yuzden
    # guard da orada bakmalidir. Test veritabanina bakilsaydi kilit HIC
    # gorunmez ve guard SESSIZCE islevsiz kalirdi (PG S5.2.1).
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
