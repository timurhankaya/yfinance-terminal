"""Ortak test fixture'lari."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from helpers import drop_stale_schemas, schema_name
from yfin.config import Settings, get_settings
from yfin.db import create_db_engine
from yfin.models import (
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
def test_engine(settings: Settings, test_schema: str) -> Iterator[Engine]:
    """Surece ozel test semasi; kosu sonunda TAMAMEN dusurulur."""
    bootstrap = create_engine(settings.bootstrap_url())
    try:
        drop_stale_schemas(bootstrap, settings.db_test_name)
        with bootstrap.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS `{test_schema}`"))
            conn.execute(
                text(
                    f"CREATE DATABASE `{test_schema}` "
                    "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
                )
            )
            conn.commit()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"MySQL erisilemiyor: {exc}")

    engine = create_db_engine(settings, test_schema)
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        conn.execute(text(V_ACTIONS_CREATE))
        conn.execute(text(V_PRICE_BARS_REGULAR_CREATE))
        # create_all() partition'lari BILMEZ (Alembic de autogenerate
        # edemez). Migration ile ayni sabit burada da uygulanmazsa
        # price_bars testlerde PARTITION'SIZ olusur ve ne pruning ne de
        # ERROR 1526 davranisi dogrulanabilir (PB S9.2).
        for stmt in timescale_ddl():
            conn.execute(text(stmt))
        conn.commit()
    yield engine
    engine.dispose()
    with bootstrap.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS `{test_schema}`"))
        conn.commit()
    bootstrap.dispose()


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
                conn.execute(text(f"DELETE FROM `{name}`"))
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
    from sqlalchemy import create_engine, text

    from yfin.config import get_settings
    from yfin.db import SYNC_LOCK_NAME

    engine = create_engine(get_settings().db_url())
    try:
        with engine.connect() as conn:
            free = conn.execute(text("SELECT IS_FREE_LOCK(:n)"), {"n": SYNC_LOCK_NAME}).scalar()
            holder = conn.execute(text("SELECT IS_USED_LOCK(:n)"), {"n": SYNC_LOCK_NAME}).scalar()
    except Exception:  # noqa: BLE001 - MySQL yoksa asil fixture zaten skip eder
        return
    finally:
        engine.dispose()

    if free == 0:
        pytest.exit(
            f"'{SYNC_LOCK_NAME}' advisory kilidi MESGUL (connection {holder}). "
            "Baska bir sync ya da live test kosusu devam ediyor; live testler "
            "es zamanli KOSTURULAMAZ. Once o kosunun bitmesini bekleyin.",
            returncode=1,
        )
