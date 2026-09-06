"""Repo testlerine ozel fixture'lar (`settings` katmani).

`load_overrides` ve `set_setting` KENDI engine'lerini kurar
(`create_db_engine` DEGIL -- CFG S2). Bu yuzden onlari test semasina
yoneltmenin tek yolu, aldiklari bootstrap `Settings`i degistirmektir;
`load_overrides(settings)`in `settings` parametresi tam da bunun icin
sozlesmenin parcasidir (CFG S3.2).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.pool import NullPool

from yfin import config as config_mod
from yfin import settings_store
from yfin.config import SETTINGS_SOURCE_VAR, Settings


@pytest.fixture
def store_settings(
    settings: Settings,
    test_engine: Engine,
    test_schema: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Settings:
    """Yapilandirma katmanini TEST semasina yoneltir.

    IKI SEY birden gerekir ve ikincisi kolayca atlanir:

      1. Dogru VERITABANI -- `settings.db_name` test veritabanina cevrilir.
         `load_overrides(settings)`in `settings` parametresi tam da bunun
         icin sozlesmenin parcasidir (CFG S3.2).
      2. Dogru SEMA -- testler surece ozel bir PG semasinda kosar
         (`tests/conftest.py`), oysa `settings_store._engine` uretim
         yolunda `search_path` KURMAZ ve `public`e bakar. Yamanmasaydi bu
         testler `settings` tablosunu bulamaz ve "tablo yok" dalina
         duserek YANLIS NEDENLE yesil kalirdi.

    Yama URETIM koduna bir `schema` parametresi eklemekten yeglenir:
    o parametrenin tek cagirani testler olurdu.
    """

    def _schema_engine(cfg: Settings) -> Engine:
        return create_engine(
            cfg.db_url(),
            poolclass=NullPool,
            # `public` de listede: uretim yolundaki `create_db_engine` ile
            # ayni gerekce (timescaledb eklentisi oraya kurulu).
            connect_args={"options": f"-c search_path={test_schema},public"},
        )

    monkeypatch.setattr(settings_store, "_engine", _schema_engine)
    return settings.model_copy(update={"db_name": test_engine.url.database})


@pytest.fixture
def clean_settings_table(test_engine: Engine) -> Iterator[None]:
    """Her test BOS bir tabloyla baslar ve arkasini toplar.

    Satir sayisi bu tabloda bir DEGISMEZ DEGILDIR (CFG S4.1); onceki
    testten kalan bir satir "seed kac satir yazdi" iddiasini sessizce
    yanlislardi.
    """
    with test_engine.connect() as conn:
        conn.execute(text("DELETE FROM settings"))
        conn.commit()
    yield
    with test_engine.connect() as conn:
        conn.execute(text("DELETE FROM settings"))
        conn.commit()


@pytest.fixture
def db_layer_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """DB katmanini ACAR ve singleton'i sifirlar.

    `conftest.py` YF_SETTINGS_SOURCE=env kuruyor (uretim semasina
    baglanmayi onlemek icin). DB yolunu sinayan testler onu kaldirmak
    ZORUNDADIR -- ve sifirlama olmadan singleton onceki testlerden dolu
    gelir, `load_overrides` HIC cagrilmaz ve test YANLIS NEDENLE yesil
    kalir (CFG S8.2).
    """
    monkeypatch.delenv(SETTINGS_SOURCE_VAR, raising=False)
    config_mod.reset_settings()
    yield
    config_mod.reset_settings()
