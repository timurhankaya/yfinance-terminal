"""Cozum sirasi ve yukleyicinin filtreleri (CFG S3.1/S3.2). AG YOK.

DB'ye hic baglanilmaz: `settings_store.fetch_rows` yamalanir. Yamanin
`load_overrides` degil `fetch_rows` uzerinde olmasi bilinclidir --
boylece UC FILTRE gercekten kosar ve testler filtrelerin varligini
dogrular.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from yfin import config as config_mod
from yfin import settings_store
from yfin.config import SETTINGS_SOURCE_VAR, get_settings

# `.env` bu alani KURMAZ (dogrulandi); varsayilani 4. Env'de kurulu bir
# alan secilseydi "DB > env" iddiasi "DB > varsayilan"a duserdi.
KEY = "yf_max_shards"
DEFAULT = 4


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Her test temiz bir singleton ve ACIK bir DB katmani ile baslar.

    `conftest.py` YF_SETTINGS_SOURCE=env kuruyor; DB yolunu sinayan bu
    dosya onu bilincli olarak kaldirir. Sifirlama olmasaydi singleton
    onceki testten dolu gelir ve yukleyici HIC kosmazdi -- testler
    YANLIS NEDENLE yesil kalirdi.
    """
    monkeypatch.delenv(SETTINGS_SOURCE_VAR, raising=False)
    config_mod.reset_settings()
    yield
    config_mod.reset_settings()


def _rows(monkeypatch: pytest.MonkeyPatch, rows: dict[str, str] | None) -> list[int]:
    """`fetch_rows`u yamalar ve cagri sayacini dondurur."""
    calls: list[int] = []

    def fake(settings: Any) -> dict[str, str] | None:
        calls.append(1)
        return rows

    monkeypatch.setattr(settings_store, "fetch_rows", fake)
    return calls


def test_db_ezmesi_varsayilani_ezer(monkeypatch: pytest.MonkeyPatch) -> None:
    _rows(monkeypatch, {KEY: "9"})
    assert get_settings().yf_max_shards == 9


def test_db_ezmesi_env_i_ezer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Init kwargs pydantic'te env'i EZER; ayri bir precedence mantigi
    YAZILMAZ (CFG S2). Bu test o davranisin sozlesme oldugunu sabitler."""
    monkeypatch.setenv("YF_MAX_SHARDS", "7")
    _rows(monkeypatch, {KEY: "9"})
    assert get_settings().yf_max_shards == 9


def test_satir_yoksa_env_gecerlidir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YF_MAX_SHARDS", "7")
    _rows(monkeypatch, {})
    assert get_settings().yf_max_shards == 7


def test_tablo_yoksa_env_only_devam(monkeypatch: pytest.MonkeyPatch) -> None:
    """`yfin db upgrade` komutunun KENDISI get_settings() cagiriyor ve
    tablo o an henuz yoktur. Bu bir hata degildir."""
    _rows(monkeypatch, None)
    assert get_settings().yf_max_shards == DEFAULT


def test_source_env_iken_db_ye_HIC_bakilmaz(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SETTINGS_SOURCE_VAR, "env")
    calls = _rows(monkeypatch, {KEY: "9"})
    assert get_settings().yf_max_shards == DEFAULT
    assert calls == [], "YF_SETTINGS_SOURCE=env iken DB okundu"


def test_source_env_bosluk_ve_buyuk_harf_toleransli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SETTINGS_SOURCE_VAR, "  ENV \n")
    calls = _rows(monkeypatch, {KEY: "9"})
    assert get_settings().yf_max_shards == DEFAULT
    assert calls == []


def test_source_yazim_hatasi_UYARI_uretir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kurtarma amacli bir anahtarin bir yazim hatasi yuzunden SESSIZCE
    etkisiz kalmasi kabul edilemez: operator katmani kapattigini
    sanirken acik kalmis olurdu."""
    warnings: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        config_mod.log, "warning", lambda msg, **kw: warnings.append((msg, kw))
    )
    monkeypatch.setenv(SETTINGS_SOURCE_VAR, "evn")
    _rows(monkeypatch, {KEY: "9"})
    assert get_settings().yf_max_shards == 9, "yazim hatasi katmani KAPATMAMALI"
    assert warnings and "YF_SETTINGS_SOURCE" in warnings[0][0]


def test_bilinmeyen_anahtar_uyari_ile_yok_sayilir(monkeypatch: pytest.MonkeyPatch) -> None:
    """`Settings` extra="ignore" tasidigi icin bilinmeyen kwarg SESSIZCE
    yutulur; filtre olmasaydi hicbir test kirmiziya donmezdi."""
    warnings: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        settings_store.log, "warning", lambda msg, **kw: warnings.append((msg, kw))
    )
    _rows(monkeypatch, {"YF_MAX_SHARDS": "9", "hicboyle_yok": "1", KEY: "6"})
    assert get_settings().yf_max_shards == 6
    reported = {kw["setting_key"] for _, kw in warnings}
    assert reported == {"YF_MAX_SHARDS", "hicboyle_yok"}


def test_gecersiz_deger_kosuyu_HIC_BASLATMAZ(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sessiz geri dusus operatorun niyetini yok sayardi ve bunu yalniz
    log'a bakan fark ederdi (CFG S7)."""
    from pydantic import ValidationError

    _rows(monkeypatch, {KEY: "0"})
    with pytest.raises(ValidationError):
        get_settings()


def test_load_overrides_get_settings_CAGIRMAZ(monkeypatch: pytest.MonkeyPatch) -> None:
    """RecursionError citi. Yukleyici `get_settings()`in ICINDEDIR;
    `create_db_engine()` gibi `settings or get_settings()` yapan bir
    yardimci kullanilsaydi sonsuz dongu olurdu (CFG S2)."""

    def boom() -> Any:
        raise AssertionError("load_overrides get_settings() cagirdi")

    _rows(monkeypatch, {KEY: "9"})
    monkeypatch.setattr(config_mod, "get_settings", boom)
    settings_store.load_overrides(config_mod.bootstrap_settings())


def test_applied_overrides_shard_a_tasinacak_degeri_verir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parent cozer, `ShardSpec` tasir, child YENIDEN OKUMAZ (CFG S3.5)."""
    _rows(monkeypatch, {KEY: "9", "yf_news_tab": "news"})
    get_settings()
    assert config_mod.applied_overrides() == {KEY: "9", "yf_news_tab": "news"}
