"""`yfin config` komutlarinin gidis-donusu (CFG S6.2). Gercek DB.

Komutlar `bootstrap_settings()` ile baglaniyor; testler onu TEST
semasina yoneltir. Yamanin `cli_config` uzerinde olmasi bilinclidir:
`settings_store`daki fonksiyonlar `settings` parametresini komuttan alir,
yani komutun HANGI Settings'i gecirdigi de sinanmis olur.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from typer.testing import CliRunner

from yfin import cli_config
from yfin.cli_config import config_app
from yfin.config import Settings

pytestmark = pytest.mark.repo

runner = CliRunner()


@pytest.fixture
def cli(
    store_settings: Settings,
    clean_settings_table: None,
    db_layer_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[CliRunner]:
    monkeypatch.setattr(cli_config, "bootstrap_settings", lambda: store_settings)
    yield runner


def _rows(engine: Engine) -> dict[str, str]:
    with engine.connect() as conn:
        return dict(conn.execute(text("SELECT setting_key, value FROM settings")).all())


def test_set_get_gidis_donusu(cli: CliRunner, test_engine: Engine) -> None:
    assert cli.invoke(config_app, ["set", "yf_max_shards", "9"]).exit_code == 0
    result = cli.invoke(config_app, ["get", "yf_max_shards"])
    assert result.exit_code == 0
    assert "yf_max_shards = 9" in result.stdout
    assert "[db]" in result.stdout


def test_anahtar_env_bicimiyle_de_kabul_edilir(cli: CliRunner, test_engine: Engine) -> None:
    """Operator `.env` aliskanligiyla `YF_MAX_SHARDS` yazacaktir;
    reddetmek gereksiz surtunmedir (CFG S2). Satir KANONIK adla acilir."""
    assert cli.invoke(config_app, ["set", " YF_MAX_SHARDS ", "9"]).exit_code == 0
    assert _rows(test_engine) == {"yf_max_shards": "9"}


def test_gecersiz_deger_cikis_2_ve_SATIR_YAZILMAZ(
    cli: CliRunner, test_engine: Engine
) -> None:
    """Geri bildirim YAZIM ANINDA gelir; hata bir sonraki gece cron'unda
    cikmaz (CFG S1)."""
    result = cli.invoke(config_app, ["set", "yf_max_shards", "0"])
    assert result.exit_code == 2
    assert _rows(test_engine) == {}


def test_env_only_anahtar_cikis_2(cli: CliRunner, test_engine: Engine) -> None:
    assert cli.invoke(config_app, ["set", "db_host", "evil"]).exit_code == 2
    assert _rows(test_engine) == {}


def test_bilinmeyen_anahtar_cikis_2(cli: CliRunner, test_engine: Engine) -> None:
    assert cli.invoke(config_app, ["set", "hicboyle_yok", "1"]).exit_code == 2
    assert _rows(test_engine) == {}


def test_unset_var_olmayan_satirda_cikis_0(cli: CliRunner) -> None:
    """Idempotent: var olmayan bir satiri silmek bir HATA DEGILDIR."""
    result = cli.invoke(config_app, ["unset", "yf_max_shards"])
    assert result.exit_code == 0
    assert "zaten satir yoktu" in result.stdout


def test_unset_sonrasi_gecerli_olacak_deger_BASILIR(
    cli: CliRunner, test_engine: Engine
) -> None:
    cli.invoke(config_app, ["set", "yf_max_shards", "9"])
    result = cli.invoke(config_app, ["unset", "yf_max_shards"])
    assert result.exit_code == 0
    assert "artik gecerli olacak deger: 4" in result.stdout
    assert _rows(test_engine) == {}


def test_list_changed_ETKIN_DEGERI_sorar(cli: CliRunner) -> None:
    """`--changed` = etkin deger model varsayilanindan farkli olanlar --
    "satiri var mi" DEGIL (CFG S6.2). Varsayilanla AYNI degeri yazan bir
    satir `--changed`te GORUNMEZ ama `--source db`de gorunur."""
    cli.invoke(config_app, ["set", "yf_max_shards", "4"])  # varsayilanin AYNISI
    changed = cli.invoke(config_app, ["list", "--changed"])
    assert "yf_max_shards" not in changed.stdout
    from_db = cli.invoke(config_app, ["list", "--source", "db"])
    assert "yf_max_shards" in from_db.stdout


def test_list_satirsiz_anahtarlari_ISARETLER(cli: CliRunner) -> None:
    """Goc sonrasi `.env` katmani fiilen bostur; satirsiz bir anahtar
    dogrudan model varsayilanina duser (CFG S5.4)."""
    result = cli.invoke(config_app, ["list", "--group", "shard"])
    assert "* yf_max_shards" in result.stdout


def test_export_varsayilan_yalniz_SATIRI_OLANLARI_verir(cli: CliRunner) -> None:
    """Yani seed dosyasinin dogal tersi. `--all` model varsayilanlarini
    da icerir ve depoya YAZILMAMALIDIR (CFG S9)."""
    import json

    cli.invoke(config_app, ["set", "yf_max_shards", "9"])
    result = cli.invoke(config_app, ["export"])
    assert json.loads(result.stdout) == {"yf_max_shards": 9}

    all_result = cli.invoke(config_app, ["export", "--all"])
    from yfin.config import DB_MANAGED_FIELDS

    assert set(json.loads(all_result.stdout)) == DB_MANAGED_FIELDS


def test_schema_DB_YE_BAKMAZ(cli: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """Saf sema, agsiz ve DB'siz calisir (CFG S6.4)."""
    monkeypatch.setattr(
        cli_config, "fetch_rows", lambda s: pytest.fail("schema DB'ye baglandi")
    )
    result = cli.invoke(config_app, ["schema"])
    assert result.exit_code == 0
    assert "yf_max_shards" in result.stdout


def test_source_env_iken_list_UYARIR_ve_ETKIN_degeri_gosterir(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DB katmani kapaliyken tablodaki satir ETKIN DEGILDIR; `list` onu
    etkinmis gibi gostermek yerine gercekte gecerli olan degeri basar ve
    basliga uyari koyar (CFG S6.2). Aksi halde kurtarma sirasindaki
    operator, kapattigini sandigi katmanin degerlerini okurdu."""
    cli.invoke(config_app, ["set", "yf_max_shards", "9"])
    monkeypatch.setenv("YF_SETTINGS_SOURCE", "env")

    result = cli.invoke(config_app, ["list", "--group", "shard"])
    assert result.exit_code == 0
    assert "DB katmani KAPALI" in result.stderr
    assert "* yf_max_shards" in result.stdout
    assert "default" in result.stdout


def test_source_env_iken_set_YINE_DE_yazar(cli: CliRunner, test_engine: Engine,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """Kurtarma senaryosu tam olarak budur: katman kapaliyken bozuk
    degeri duzeltebilmek gerekir (CFG S6.2)."""
    monkeypatch.setenv("YF_SETTINGS_SOURCE", "env")
    assert cli.invoke(config_app, ["set", "yf_max_shards", "9"]).exit_code == 0
    assert _rows(test_engine) == {"yf_max_shards": "9"}
