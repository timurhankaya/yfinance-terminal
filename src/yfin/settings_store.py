"""`settings` tablosunun okunmasi, dogrulanmasi ve yazilmasi (CFG S3.2/S5/S6.3).

Bu modul yapilandirma katmaninin TEK yazma kapisidir. Dogrulama CLI
komutunun icine gomulmedi cunku gelecek yonetim paneli o komutu ATLAR ve
dogrudan ham SQL'e duserdi -- CFG S7 bunu felaket senaryosu sayiyor.
`yfin config set` bu modulun INCE bir sarmalayicisidir; panel AYNI
fonksiyonu cagirir.

Dogrulama, OKUMANIN kullandigi kod yolunun aynisidir: deger
`Settings(**{**mevcut_ezmeler, key: value})` kurularak sinanir. Iki ayri
dogrulama yazilsaydi biri gevser ve panel "gecerli" dedigi degeri bir
sonraki kosuda coktururdu.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import sqlalchemy
from pydantic import ValidationError
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from yfin.config import (
    DB_MANAGED_FIELDS,
    ENV_ONLY_FIELDS,
    Settings,
    bootstrap_settings,
    settings_from_overrides,
)
from yfin.logging_setup import get_logger
from yfin.models.settings import SettingRow

log = get_logger(__name__)

TABLE_NAME = "settings"
SEED_PATH = Path("config/settings.seed.json")


class SettingRejected(ValueError):
    """Deger YAZILMADI. CLI bunu cikis kodu 2'ye cevirir.

    Cikis kodu 2 kod tabaninda "yapilandirma reddi" anlamina gelir
    (`PruneDisabledError` deseni); 1 ile ayrilmasi, bir CI adiminin
    "gecersiz ayar" ile "komut hata verdi"yi ayirt edebilmesi icindir.
    """


class Source(StrEnum):
    """Etkin degerin NEREDEN geldigi."""

    DB = "db"
    ENV = "env"
    DEFAULT = "default"


@dataclass(frozen=True)
class SettingState:
    """Tek bir anahtarin ETKIN durumu.

    `has_row` `source is Source.DB` ile AYNI SEY DEGILDIR: bir anahtarin
    satiri olabilir ama `ENV_ONLY_FIELDS` filtresine takilip
    uygulanmamis olabilir. Ikisini tek bayrakla temsil etmek o durumu
    gorunmez kilardi.
    """

    key: str
    value: str
    source: Source
    has_row: bool


# --- serilestirme (CFG S4.4) ----------------------------------------------


def serialize(value: Any) -> str:
    """Python degeri -> `value` sutununun metni.

    DB-yonetimli alanlarin tamami skalerdir (bool / float / int / str).
    Karmasik tip
    icin kural TANIMLANMAMISTIR cunku boyle bir alan yoktur; skaler citi
    (`tests/unit/test_settings_split.py`) biri eklenirse patlar ve bu
    fonksiyonun guncellenmesini ZORUNLU kilar.
    """
    if isinstance(value, bool):
        # "True" YAZILMAZ: pydantic onu da cozer ama `.env` bicimiyle
        # gidis-donus esitligi (`seed(export(state)) == state`) bozulurdu.
        return "true" if value else "false"
    return str(value)


def normalize_key(raw: str) -> str:
    """Operator girdisini kanonik anahtara cevirir.

    Kanonik bicim MODEL ALAN ADIDIR (kucuk harf). Operator `.env`
    aliskanligiyla `YF_MAX_SHARDS` yazacaktir; bunu reddetmek gereksiz
    surtunmedir. Tablo collation'inin duyarli olmasi bu normalizasyonla
    CELISMEZ: amac cakismayi onlemek degil, ham SQL ile sokulmus
    `YF_MAX_SHARDS` satirinin AYRI ve GORUNUR kalip "bilinmeyen anahtar"
    uyarisina takilmasidir (CFG S2).
    """
    return raw.strip().lower()


# --- okuma ----------------------------------------------------------------


def _engine(settings: Settings) -> Engine:
    """`create_db_engine` DEGIL (CFG S2).

    Uc gerekce: (1) argumansiz cagrilirsa `settings or get_settings()`
    yuzunden RecursionError uretir -- yukleyici zaten `get_settings()`in
    ICINDEDIR; (2) havuz boyutlandirmasi ve `pool_pre_ping` TEK bir
    SELECT icin olu agirliktir; (3) `connect_timeout` olmadan DB
    erisilemezken CLI onlarca saniye asili kalir. Depo bu deseni zaten
    kullaniyor (`migrations/env.py` -> `poolclass=pool.NullPool`).
    """
    return create_engine(
        settings.db_url(),
        poolclass=NullPool,
        connect_args={"connect_timeout": 5},
    )


def fetch_rows(settings: Settings) -> dict[str, str] | None:
    """Tablodaki HAM satirlar; tablo yoksa `None`.

    Tablonun varligi `inspect(engine).has_table()` ile sinanir, hata
    koduna (MySQL 1146 / PG 42P01) BAKILMAZ: kod kontrolu motora
    baglidir ve motor degistiginde SESSIZCE yanlis olurdu.

    Yetki hatasi ve erisilemeyen veritabani YUKSELIR. Sessizce env-only
    devam etmek, yanlis yapilandirmayla kosmak demektir (CFG S7).
    """
    engine = _engine(settings)
    try:
        if not sqlalchemy.inspect(engine).has_table(TABLE_NAME):
            # `yfin db upgrade` komutunun KENDISI get_settings() cagiriyor
            # ve tablo o an henuz yoktur; bu bir hata degil normal bir
            # kurulum anidir.
            log.info("settings tablosu yok; yalniz env kullaniliyor")
            return None
        with Session(engine) as session:
            rows = session.execute(select(SettingRow.setting_key, SettingRow.value)).all()
        return {key: value for key, value in rows}
    finally:
        engine.dispose()


class KeyVerdict(StrEnum):
    """Bir anahtarin DB katmaninda YERI VAR MI."""

    OK = "ok"
    ENV_ONLY = "env_only"
    UNKNOWN = "unknown"


def classify_key(key: str) -> KeyVerdict:
    """Anahtar politikasinin TEK dogruluk kaynagi.

    Okuma yolu (`filter_overrides`) ve yazma yolu (`validate_pair`) ayni
    karari vermek ZORUNDADIR. Karar iki yerde kodlanmis olsaydi -- ilk
    yazimda oyleydi -- ileride eklenen ucuncu bir kategori (ornegin
    kullanimdan kaldirilmis anahtarlar) birinde unutulabilirdi. Yazma
    yolunda unutmak yalnizca can sikici olurdu; OKUMA yolunda unutmak
    GUVENLIK SINIRINI delerdi: bir DB satiri `db_host`u degistirip
    baglantiyi baska yere cevirebilir ya da `yf_proxy_secret_key`i
    ezebilirdi.
    """
    if key in ENV_ONLY_FIELDS:
        return KeyVerdict.ENV_ONLY
    if key not in DB_MANAGED_FIELDS:
        return KeyVerdict.UNKNOWN
    return KeyVerdict.OK


# Karar ORTAK, mesajlar AYRI: ikisi ayni siddette degildir. Env-only bir
# satir bir guvenlik olayidir ("reddedildi"); bilinmeyen bir anahtar
# cogunlukla ham SQL ile sokulmus kanonik olmayan bir addir ("yok
# sayildi").
_LOG_MESSAGE = {
    KeyVerdict.ENV_ONLY: "settings satiri REDDEDILDI: env-only alan DB'den ezilemez",
    KeyVerdict.UNKNOWN: "settings satiri yok sayildi: bilinmeyen anahtar",
}
_REJECT_MESSAGE = {
    KeyVerdict.ENV_ONLY: (
        "{key} env-only bir alandir ve DB'den yonetilemez "
        "(baglanti bilgileri, Fernet anahtari, log seviyesi)."
    ),
    KeyVerdict.UNKNOWN: "bilinmeyen ayar anahtari: {key}",
}


def filter_overrides(rows: Mapping[str, str]) -> dict[str, str]:
    """Uygulanabilir satirlar. Elenen HICBIRI sessiz gecmez.

    Gecersiz DEGER burada yakalanmaz: bu fonksiyon ham metin dondurur ve
    hata `Settings(**overrides)` cagrisinda `ValidationError` olarak
    dogar. Boylece DEGER dogrulamasi tek yerde kalir (CFG S3.2), ANAHTAR
    politikasi ise `classify_key`te.
    """
    out: dict[str, str] = {}
    for key, value in rows.items():
        verdict = classify_key(key)
        if verdict is KeyVerdict.OK:
            out[key] = value
        else:
            # `Settings` extra="ignore" tasiyor ve bilinmeyen kwarg'i
            # SESSIZCE yutuyor (canli dogrulandi); bu uyari bir "iyi
            # olur" degil ZORUNLULUKTUR -- atlanirsa hicbir test
            # kirmiziya donmez.
            log.warning(_LOG_MESSAGE[verdict], setting_key=key)
    return out


def load_overrides(settings: Settings) -> dict[str, str]:
    """Uygulanacak DB ezmeleri (ham metin).

    Bootstrap `Settings`i PARAMETRE ALIR; bu bir test kolayligi degil
    sozlesmenin parcasidir (CFG S3.2): repo testleri onu test semasina
    yoneltebilsin ve `get_settings()` cagrilmasin diye -- cagrilsaydi
    yukleyici kendi kendini cagirir ve RecursionError uretirdi.
    """
    rows = fetch_rows(settings)
    if rows is None:
        return {}
    return filter_overrides(rows)


def settings_state(*, rows: Mapping[str, str] | None = None) -> dict[str, SettingState]:
    """DB-yonetimli her anahtarin ETKIN degeri ve kaynagi.

    `Settings` PARAMETRESI YOKTUR ve bu bilinclidir. Ilk yazimda vardi,
    kullanilmiyordu ve imza YALAN SOYLUYORDU: cagiran (ozellikle repo
    testi) onu vererek okumayi yonlendirdigini saniyordu, oysa okuma
    tamamen `rows`tan geliyor. Baglantiyi kim acacaksa `fetch_rows`u O
    cagirir; bu fonksiyon SAFTIR.

    `rows=None` "DB'ye BAKILMADI" demektir (erisilemedi ya da
    `YF_SETTINGS_SOURCE=env`); o durumda kaynak env/default olur ve
    `yfin config list` COKMEZ. Kurtarma komutu, kurtarmaya calistigi
    arizaya kurban gitmemelidir (CFG S6.2).

    Kaynak ayrimi `model_fields_set` ile yapilir, "deger varsayilandan
    farkli mi" karsilastirmasiyla DEGIL: `.env`de varsayilanla AYNI degeri
    yazan bir kurulum aksi halde `default` gorunurdu.
    """
    overrides = filter_overrides(rows) if rows is not None else {}
    env_only = bootstrap_settings()
    resolved = settings_from_overrides(overrides) if overrides else env_only

    out: dict[str, SettingState] = {}
    for key in sorted(DB_MANAGED_FIELDS):
        if key in overrides:
            source = Source.DB
        elif key in env_only.model_fields_set:
            source = Source.ENV
        else:
            source = Source.DEFAULT
        out[key] = SettingState(
            key=key,
            value=serialize(getattr(resolved, key)),
            source=source,
            has_row=rows is not None and key in rows,
        )
    return out


def export_values(
    states: Mapping[str, SettingState], *, all_keys: bool = False
) -> dict[str, Any]:
    """`yfin config export`in JSON govdesi. SAF fonksiyon.

    Degerler NATIVE tiple doner (int / bool / float / str), metin degil:
    `seed(export(state)) == state` gidis-donus garantisi buna dayanir
    (CFG S4.4/S8.2) ve tohum dosyasi da native tip kullanir.

    CLI'nin icine gomulu birakilmisti; repo testi ayni uc satiri
    KOPYALAMAK zorunda kaldi ve o kopya, ciktinin dogrulugunu sinamak
    yerine kendi kendini sinar hale geldi. Buraya cikarilinca hem tek
    dogruluk kaynagi oldu hem de DB'siz test edilebildi.
    """
    resolved = settings_from_overrides({key: state.value for key, state in states.items()})
    return {
        key: getattr(resolved, key)
        for key, state in states.items()
        if all_keys or state.has_row
    }


# --- dogrulama ------------------------------------------------------------


def validate_pair(key: str, value: str, *, overrides: Mapping[str, str]) -> None:
    """Tek bir (anahtar, deger) ciftini reddeder ya da sessizce gecer.

    Diger ezmeler de kurulumda yer alir cunku dogrulama OKUMANIN kod
    yolunun aynisi olmak zorundadir; ileride alanlar arasi bir validator
    eklenirse bu cagri onu da yakalar.
    """
    verdict = classify_key(key)
    if verdict is not KeyVerdict.OK:
        raise SettingRejected(_REJECT_MESSAGE[verdict].format(key=key))
    candidate = {**overrides, key: value}
    try:
        settings_from_overrides(candidate)
    except ValidationError as exc:
        raise SettingRejected(f"{key} icin gecersiz deger {value!r}: {_first_error(exc)}") from exc


def _first_error(exc: ValidationError) -> str:
    errors = exc.errors()
    return str(errors[0].get("msg", exc)) if errors else str(exc)


# --- yazma ----------------------------------------------------------------


def _upsert(session: Session, key: str, value: str) -> None:
    """Var olan satiri gunceller, yoksa ekler.

    Motora ozgu bir upsert deyimi (`ON CONFLICT` / `ON DUPLICATE KEY`)
    KULLANILMAZ: bu tablo tek bir operatorun (ya da panelin) dokundugu,
    on satirlik bir tablodur ve motor notrlugu (CFG S2) ucuza korunur.
    """
    row = session.get(SettingRow, key)
    if row is None:
        session.add(SettingRow(setting_key=key, value=value))
    else:
        row.value = value


def set_setting(key: str, value: str, *, settings: Settings) -> str:
    """Dogrular ve YAZAR. Kanonik anahtari dondurur.

    Gecersiz deger YAZIM ANINDA reddedilir; hata bir sonraki gece
    cron'unda cikmamalidir (CFG S1).
    """
    canonical = normalize_key(key)
    validate_pair(canonical, value, overrides=load_overrides(settings))
    engine = _engine(settings)
    try:
        with Session(engine) as session:
            _upsert(session, canonical, value)
            session.commit()
    finally:
        engine.dispose()
    return canonical


def unset_setting(key: str, *, settings: Settings) -> bool:
    """Satiri siler. Satir yoksa `False` -- HATA DEGIL (idempotent).

    Silinen anahtar `.env`e, o da yoksa model varsayilanina duser.
    """
    canonical = normalize_key(key)
    engine = _engine(settings)
    try:
        with Session(engine) as session:
            row = session.get(SettingRow, canonical)
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True
    finally:
        engine.dispose()


def write_all(plan: Mapping[str, str], *, settings: Settings) -> None:
    """Plani TEK transaction'da yazar (ya hep ya hic, CFG S5.2)."""
    if not plan:
        return
    engine = _engine(settings)
    try:
        with Session(engine) as session:
            for key, value in plan.items():
                _upsert(session, key, value)
            session.commit()
    finally:
        engine.dispose()


# --- tohum (CFG S5) -------------------------------------------------------


def load_seed_file(path: Path = SEED_PATH) -> dict[str, Any]:
    """`config/settings.seed.json` -- BU KURULUMUN yapilandirmasi.

    Dosya TAM LISTE olmak zorunda DEGILDIR ve olmamalidir: yalnizca
    varsayilandan sapmak istenen anahtarlar yazilir. Yazilmayan bir
    anahtarin degeri model varsayilanindan gelir ve `Settings`teki
    varsayilan degistiginde kuruluma YANSIR -- tam liste yazilsaydi bu
    bag kopardi (CFG S5.1).
    """
    if not path.exists():
        raise SettingRejected(f"tohum dosyasi bulunamadi: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SettingRejected(f"tohum dosyasi bir JSON nesnesi olmali: {path}")
    return data


def plan_seed(
    seed: Mapping[str, Any],
    existing: Mapping[str, str],
    *,
    force: bool = False,
    adopt_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Yazilacak satirlar. SAF fonksiyon: DB'ye BAKMAZ.

    Kapsam YALNIZCA JSON'daki anahtarlardir:

        JSON'daki anahtarin satiri YOK -> JSON degeri yazilir
        JSON'daki anahtarin satiri VAR -> DOKUNULMAZ (--force ile ezilir)
        JSON'da OLMAYAN anahtar        -> HICBIR SEY (--force dahil)

    Son satir kritiktir. Ilk taslak `seed`i "tum eksik satirlari doldur"
    olarak tanimliyordu; o tanimla `seed` `unset`i SESSIZCE geri alir,
    yani iki komut birbirinin isini bozardi (CFG S5.3). `--force` da
    yalnizca JSON anahtarlarini ezer; aksi halde operatorun panelden
    yaptigi TUM ezmeleri sessizce silerdi.

    YA HEP YA HIC: once JSON'un tamami dogrulanir, sonra tek
    transaction'da yazilir. Yarim yazilmis bir tohum, hangi anahtarin
    hangi kaynaktan geldigini belirsiz birakirdi.
    """
    plan: dict[str, str] = {}
    validated: dict[str, str] = {}
    for raw_key, raw_value in seed.items():
        key = normalize_key(str(raw_key))
        value = serialize(raw_value)
        validate_pair(key, value, overrides=validated)
        validated[key] = value

    for key, value in validated.items():
        if force or key not in existing:
            plan[key] = value

    if adopt_env is not None:
        # Bir kereligine, GOC icin (CFG S5.2). `.env`inde YF_MAX_SHARDS=8
        # olan bir kurulum bu adim olmadan migration sonrasi sessizce
        # 4'e donerdi.
        for key, value in adopt_env.items():
            if key in validated or key in existing:
                continue
            plan[key] = value
    return plan


def adopt_env_values() -> dict[str, str]:
    """DB katmani DEVRE DISI bir bootstrap'tan okunan ETKIN degerler.

    `get_settings()` KULLANILMAZ: kullanilsaydi tohumlama kendi yazdigi
    satirlari geri besler ve `--adopt-env` "env'i devral" olmaktan cikip
    "DB'yi DB'den kopyala"ya donerdi (CFG S5.2).
    """
    env_only = bootstrap_settings()
    return {key: serialize(getattr(env_only, key)) for key in sorted(DB_MANAGED_FIELDS)}
