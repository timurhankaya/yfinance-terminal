"""Uygulama yapilandirmasi (CFG).

Iki katman vardir:

  1. `.env` + model varsayilani -- pydantic-settings'in kendi yolu.
  2. `settings` TABLOSU -- 39 alan icin DB ezmesi (CFG S3.1).

Cozum sirasi `CLI bayragi > settings tablosu > .env > model varsayilani`
olarak KENDILIGINDEN dogar: `Settings(**overrides)` cagrisinda init
kwargs'lari pydantic env degerlerini EZER (canli dogrulandi). Ayri bir
precedence mantigi YAZILMAZ -- yazilsaydi pydantic'inkiyle sessizce
ayrisabilirdi.

Metadata (tip / varsayilan / aralik / aciklama / grup) BU DOSYADA yasar,
`settings` tablosunda DEGIL (CFG S2): kopyalansaydi `Field(ge=1)` bir gun
`ge=2` olur ve tablodaki kopya bayatlardi. Yonetim paneli formu
`settings_schema()` ile buradan cizer.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

from yfin.logging_setup import configure_logging, get_logger

log = get_logger(__name__)

# `Settings` alani DEGILDIR ve olmamalidir: DB katmanini kapatan anahtar,
# DB katmanindan okunamaz (tavuk-yumurta). `os.getenv` ile okunur.
SETTINGS_SOURCE_VAR = "YF_SETTINGS_SOURCE"

# Panelin gruplayacagi 11 kume. Yeni bir grup adi eklemek bilincli bir
# karardir; `tests/unit/test_settings_split.py` bilinmeyen grubu reddeder.
SETTING_GROUPS: tuple[str, ...] = (
    "client",
    "runner",
    "shard",
    "proxy",
    "symbols",
    "datasets",
    "market",
    "domain",
    "discovery",
    "bars",
    "maintenance",
)


def _cfg(group: str, description: str, **kwargs: Any) -> Any:
    """`Field` + panel metadatasi.

    `group` `json_schema_extra`ya konur cunku pydantic'in `FieldInfo`su
    serbest anahtar KABUL ETMEZ. Ayri bir modul-seviyesi sozluk
    tutulsaydi alan ile grubu iki ayri yerde yasar ve biri digerini
    unutabilirdi; burada alanin TANIMIYLA ayni satirdadir.
    """
    return Field(description=description, json_schema_extra={"group": group}, **kwargs)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = "yfin"
    db_password: str = ""
    db_name: str = "yfinance"
    # AYRI BIR VERITABANI, sema degil: canli veriyle test verisini fiziksel
    # olarak ayirir. Surece ozel test SEMALARI bunun ICINDE acilir (S9.1).
    db_test_name: str = "yfinance_test"

    yf_rate_limit_per_sec: float = _cfg(
        "client", "Saniyedeki istek tavani (token bucket).", default=2.0, gt=0
    )
    yf_max_workers: int = _cfg(
        "runner", "Es zamanli sembol worker thread sayisi.", default=4, ge=1
    )
    yf_queue_maxsize: int = _cfg(
        "runner", "Sembol kuyrugunun tavani (geri basinc).", default=8, ge=1
    )
    yf_retry_attempts: int = _cfg(
        "client", "Gecici hatada toplam deneme sayisi.", default=5, ge=1
    )
    yf_retry_initial_sec: float = _cfg(
        "client", "Ustel geri cekilmenin ilk bekleme suresi (saniye).", default=1.0, ge=0
    )
    yf_retry_max_sec: float = _cfg(
        "client", "Ustel geri cekilmenin bekleme tavani (saniye).", default=16.0, ge=0
    )
    yf_news_count: int = _cfg(
        "datasets", "Sembol basina cekilecek haber sayisi.", default=50, ge=1
    )
    yf_news_tab: str = _cfg(
        "datasets", "Yahoo haber sekmesi (all / news / press releases).", default="all"
    )
    yf_incremental_overlap_days: int = _cfg(
        "symbols", "Artimli cekimde geriye ortusme penceresi (gun).", default=7, ge=0
    )
    yf_delist_threshold: int = _cfg(
        "symbols",
        "is_active=0 yapan ardisik unknown_symbol sayisi (unknown_streak esigi).",
        default=5,
        ge=1,
    )

    # --- shard'li calistirma (proxy havuzu) --------------------------------
    yf_max_shards: int = _cfg(
        "shard", "Ayni anda kosacak shard (proxy basina process) ust siniri.", default=4, ge=1
    )
    yf_shard_timeout_seconds: int = _cfg(
        "shard",
        "Bir shard process'inin tamamlanma suresi tavani (saniye).",
        default=3600,
        ge=60,
    )
    yf_txn_retry_attempts: int = _cfg(
        "runner", "Deadlock/kilit zaman asiminda transaction deneme sayisi.", default=3, ge=1
    )

    # --- proxy politikasi -------------------------------------------------
    yf_proxy_cooldown_seconds: int = _cfg(
        "proxy", "Esigi asan proxy'nin dinlendirilme suresi (saniye).", default=900, ge=1
    )
    yf_proxy_failure_threshold: int = _cfg(
        "proxy", "Cooldown'a dusuren ardisik hata sayisi.", default=3, ge=1
    )
    yf_proxy_dead_rounds: int = _cfg(
        "proxy", "Proxy'yi olu sayan ardisik cooldown turu sayisi.", default=3, ge=1
    )
    yf_proxy_check_timeout: float = _cfg(
        "proxy", "Proxy saglik kontrolu istek zaman asimi (saniye).", default=10.0, gt=0
    )
    # Fernet anahtari; bossa parolali proxy EKLENEMEZ (sifre koda/DB'ye
    # duz metin girmez)
    yf_proxy_secret_key: str = ""

    # --- yfinance advanced ------------------------------------------------
    # yfinance'in tz/cookie/ISIN cache'i SQLite'tir; her shard kendi
    # dizinini alir, aksi halde ikinci yazar SQLITE_BUSY yer
    yf_tz_cache_dir: str = _cfg(
        "datasets", "yfinance tz/cookie/ISIN cache kok dizini.", default=".cache/yfinance"
    )
    yf_history_repair: bool = _cfg(
        "datasets", "history(repair=True): bolunme/para birimi hatalarini onar.", default=True
    )

    # Financials / market (S11)
    yf_earnings_dates_max_pages: int = _cfg(
        "datasets", "earnings_dates icin sayfa ust siniri.", default=3, ge=1
    )
    # esgScores 19 sembolun 19'unda da 404 verdi; izleme dataset'i
    # VARSAYILAN KAPALIDIR (AH S6.3). Acilirsa her sembolde bir bosa istek
    # ve bir `empty` hucre uretir.
    yf_probe_sustainability: bool = _cfg(
        "datasets",
        "esgScores izleme probu; dataset artik opt-in kayitlidir "
        "(--datasets sustainability ile kosar).",
        default=False,
    )
    yf_market_regions: str = _cfg(
        "market",
        "Piyasa ozeti bolgeleri (virgullu).",
        default="US,EUROPE,ASIA,GB,CURRENCIES,CRYPTOCURRENCIES,COMMODITIES,RATES",
    )
    yf_calendar_lookback_days: int = _cfg(
        "market", "Takvim penceresinin geriye uzanimi (gun).", default=7, ge=0
    )
    yf_calendar_lookahead_days: int = _cfg(
        "market", "Takvim penceresinin ileriye uzanimi (gun).", default=30, ge=0
    )
    yf_calendar_page_limit: int = _cfg(
        "market", "Takvim sayfa boyu; Yahoo tavani 100.", default=100, ge=1, le=100
    )
    yf_calendar_max_pages: int = _cfg(
        "market", "Takvim icin sayfa ust siniri.", default=5, ge=1
    )

    # --- sektor / endustri (SI S12) ---------------------------------------
    # ISO 3166-1 alpha-2, virgullu. ILKI BIRINCILDIR: bolgesiz dataset'ler
    # onun yanitini kullanir ve dogrulama probu onu taban alir.
    #
    # IKI AYAR, UC DEGIL. Ilk taslakta bir `yf_domain_include_reports`
    # bayragi vardi; cikarildi cunku hicbir yerde okunmuyordu ve okunsaydi
    # profil dataset'inin `produces`'ini KOSULLU yapar, denetimin hucre
    # sayisini yapilandirmaya bagimli kilardi. Rapor istemeyen kullanici
    # `--datasets sector_rankings,industry_rankings` diyebilir.
    yf_domain_regions: str = _cfg(
        "domain", "Sektor/endustri bolgeleri (virgullu, ILKI BIRINCILDIR).", default="US"
    )
    # Bolge dogrulama probunun referans sektoru (SI S6.6)
    yf_domain_reference_sector: str = _cfg(
        "domain", "Bolge dogrulama probunun referans sektoru.", default="technology"
    )

    # --- kesif: Search / Lookup / Screener (SQ S13.1) ---------------------
    # YF_DISCOVERY_ENABLED ANAHTARI YOKTUR ve bilincli olarak
    # KALDIRILMISTIR. Ilk tasarimda `search`/`lookup` kaydini bir bayraga
    # baglamisti (`sustainability` deseni). Uygulamada iki kusuru gorundu:
    #   1. Bayrak kapaliyken `--datasets search` de calismiyordu -- dataset
    #      registry'de hic yoktu.
    #   2. Bayrak acildigi anda CIPLAK `yfin sync` de onlari cekmeye
    #      basliyordu: +9.000 istek/gun. Yani bayrak tuzagi cozmuyor,
    #      yalnizca kullanici onu acana kadar erteliyordu.
    # Cozum registry'ye tasindi: `register(..., opt_in=True)` -- adiyla
    # istendiginde kosar, `all` genislemesinde HIC gorunmez.
    yf_search_max_results: int = _cfg(
        "discovery", "Search: donen kote sayisi.", default=10, ge=1
    )
    yf_search_news_count: int = _cfg(
        "discovery", "Search: donen haber sayisi.", default=5, ge=0
    )
    yf_search_lists_count: int = _cfg(
        "discovery", "Search: donen liste sayisi.", default=10, ge=0
    )
    # `all` cagrisinin belge tavani ~1.000 olculdu; 1000 istemek tavana
    # kadarini alir. 250 istenseydi DAR terimlerde bile kirpardi
    # (BTC: count=250 -> 248 belge, oysa total 503).
    yf_lookup_count: int = _cfg(
        "discovery", "Lookup: tek cagrida istenen belge sayisi.", default=1000, ge=1
    )
    # `lookupTotals.all` bu degeri asarsa `all` cagrisi KIRPILMIS demektir
    # ve yedi tipli dala gecilir (SQ K6). Olculdu: BTC 503 -> `all` tam
    # kume; GOLD 7.273 -> `all` yalniz 995 belge, tipli birlesim 3.313.
    # Esik `all`in gozlenen tavaninin (~1.000) ALTINDA tutulur ki kirpilma
    # BASLAMADAN tipli dala gecilsin.
    yf_lookup_all_threshold: int = _cfg(
        "discovery", "Bu esik asilirsa `all` yerine tipli dala gecilir.", default=500, ge=1
    )
    # Yahoo tavani 250; asilirsa `yf.screen` ValueError firlatir. le=250
    # sayesinde bu bir YAPILANDIRMA hatasi olur ve `Settings` yuklenirken
    # gorulur, kosu ortasinda degil.
    yf_screen_size: int = _cfg(
        "discovery", "Screener sayfa boyu; Yahoo tavani 250.", default=250, ge=1, le=250
    )
    # Ekran basina sayfa ust siniri. 19 predefined: sinirsiz 49 istek,
    # cap=4 ile 32. En pahali ekran `most_shorted_stocks` (total 4.022,
    # 17 sayfa). Sinira takilan ekran `screen_runs.total` ile
    # `fetched_rows` farkindan GORULUR (SQ S9.6/1).
    yf_screen_max_pages: int = _cfg(
        "discovery", "Ekran basina sayfa ust siniri.", default=4, ge=1
    )
    # Bos = `screens.py`deki tum ekranlar (DB'de kapatilmis olanlar haric).
    yf_screen_keys: str = _cfg(
        "discovery", "Kosulacak ekran anahtarlari (virgullu; bos = hepsi).", default=""
    )

    # --- price_bars (PB S11) ----------------------------------------------
    # YF_BAR_INTERVALS ANAHTARI YOKTUR ve bilincli olarak eklenmemistir.
    # Spec'te vardi; denetimde hicbir yerde okunmadigi gorulunce KALDIRILDI
    # (YAGNI). Iki gerekce:
    #   1. Ikinci bir dogruluk kaynagi yaratirdi: "hangi interval'ler
    #      kosuyor" sorusunun cevabi hem registry alias'inda hem .env'de
    #      olurdu ve ikisi sessizce ayrisabilirdi.
    #   2. Karsiladigi ihtiyac zaten karsilanmis durumda: kullanici
    #      `--datasets bars_5m,bars_15m` yazabilir ya da `intraday`
    #      alias'ini kullanabilir.
    # Seans disi barlar yazilsin mi. DIKKAT: ek bar yalniz ABD
    # hisselerinde gelmez - SHEL.L ve VWCE.DE hasPrePostMarketData=False
    # bildirdikleri halde 5 ve 8 ek bar donduruyor (olculdu). Kapatmak,
    # o barlari KALICI olarak kaybetmek demektir.
    yf_bar_prepost: bool = _cfg(
        "bars",
        "Seans disi barlari da yaz (kapatmak o barlari KALICI kaybettirir).",
        default=True,
    )
    # Artimli pencerede geriye ortusme. Ortusme idempotenttir (olculdu:
    # 78 ortak barda deger farki yok); maliyeti birkac yuz gereksiz
    # upsert, faydasi seans sinirindaki barin kacmamasi.
    yf_bar_overlap_days: int = _cfg(
        "bars", "Artimli bar penceresinde geriye ortusme (gun).", default=2, ge=0
    )

    # Budama VARSAYILAN KAPALI (S7.4): takvim ve _history satirlari yeniden
    # cekilemez, bu yuzden silme acikca etkinlestirilmeden calismaz.
    yf_prune_enabled: bool = _cfg(
        "maintenance", "Budama ana anahtari; VARSAYILAN KAPALI (S7.4).", default=False
    )

    log_level: str = "INFO"

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

        PostgreSQL'de veritabani SECMEDEN baglanilamaz, bu yuzden bakim
        veritabani `postgres` kullanilir.

        BU URL YALNIZCA `CREATE DATABASE` ICINDIR. `information_schema` ve
        `pg_namespace` VERITABANINA OZELDIR: buradan acilan bir baglanti
        `yfinance_test` icindeki semalari GOREMEZ. Sema olusturma, silme
        ve bayat sema temizligi `db_url(db_test_name)` ile yapilmalidir
        (S9.1) -- aksi halde temizlik sessizce hicbir sey yapar ve
        semalar sonsuza kadar birikir.
        """
        return self.db_url(database="postgres")


# --- alan bolunmesi (CFG S3.1) --------------------------------------------

ENV_ONLY_FIELDS = frozenset(
    {
        # Bootstrap paradoksu: baglantiyi ACAN deger baglantinin ardinda
        # duramaz.
        "db_host",
        "db_port",
        "db_user",
        "db_password",
        "db_name",
        "db_test_name",
        # Fernet anahtarini, SIFRELEDIGI proxy parolalariyla ayni yere
        # koymak proxy/crypto.py'yi anlamsizlastirirdi.
        "yf_proxy_secret_key",
        # configure_logging(), create_db_engine()'den ONCE cagriliyor
        # (shard.py). Ayrica CFG S3.3'un log sirasi bu alanin env'de
        # olmasina DAYANIR: yukleyicinin uyarilari -- guvenlik siniri
        # dahil -- yapilandirilmis logger'a dusmek zorundadir.
        "log_level",
    }
)

# TAMLAYAN olarak turetilir: `Settings`e yeni bir alan eklendiginde
# kendiliginden DB-yonetimli olur. Liste elle yazilsaydi yeni alan
# SESSIZCE hicbir kumeye girmezdi. Turetmenin fail-open riskini
# `tests/unit/test_settings_split.py`teki DORT cit kapatir (tuketicilik,
# metadata, SIR ADI kalibi, skalerlik).
DB_MANAGED_FIELDS = frozenset(Settings.model_fields) - ENV_ONLY_FIELDS


_settings: Settings | None = None
# Yukleyicinin UYGULADIGI ham ezmeler. Shard parent'i bunlari `ShardSpec`
# ile child'a tasir (CFG S3.5); child yeniden okumaz.
_overrides: dict[str, str] = {}
# Bugun kilitsiz olmasi zararsizdi (~1 ms). DB okumasiyla birlikte iki
# worker thread IKI AYRI `Settings` NESNESI uretebilir; nesne kimligine
# dayanan test yamalari (test_client.py) sessizce etkisizlesirdi.
_lock = threading.Lock()


def source_is_env() -> bool:
    """`YF_SETTINGS_SOURCE=env` -> DB katmani HIC okunmaz.

    Deger `strip().lower()` ile karsilastirilir; `env` disindaki bos
    olmayan her deger WARNING uretir. KURTARMA amacli bir anahtarin bir
    yazim hatasi yuzunden SESSIZCE etkisiz kalmasi kabul edilemez --
    operator DB katmanini kapattigini sanirken acik kalmis olurdu.
    """
    raw = os.getenv(SETTINGS_SOURCE_VAR)
    if raw is None:
        return False
    value = raw.strip().lower()
    if value == "env":
        return True
    if value:
        log.warning(
            "YF_SETTINGS_SOURCE degeri taninmadi; DB katmani ACIK kaliyor",
            value=raw,
            expected="env",
        )
    return False


def bootstrap_settings() -> Settings:
    """DB katmanina HIC dokunmayan `Settings` ornegi.

    Uc yerde gereklidir: (a) yukleyicinin kendisi, (b) `yfin db create` /
    `yfin db revision` gibi veritabani HENUZ YOKKEN kosan komutlar,
    (c) `seed --adopt-env` -- etkin degeri okurken kendi yazdigi
    satirlari geri BESLEMEMESI icin.
    """
    return Settings()


def _load() -> Settings:
    global _overrides
    _overrides = {}
    env_only = bootstrap_settings()

    # ILK IS: `log_level` ENV_ONLY oldugu icin BURADA hazirdir. structlog
    # cache_logger_on_first_use=True ile calisir; bundan once basilan her
    # satir yapilandirilmamis PrintLogger'a duser ve redact_credentials
    # processor'i devrede olmaz (CFG S3.3).
    configure_logging(env_only.log_level)

    if source_is_env():
        return env_only

    # YEREL import: settings_store -> db/models -> config zinciri dongu
    # yapar. Kod tabaninin bu durumdaki deseni yerel import'tur
    # (prune.py).
    from yfin.settings_store import load_overrides

    overrides = load_overrides(env_only)
    if not overrides:
        return env_only
    _overrides = overrides
    # Init kwargs pydantic'te env'i EZER; cozum sirasi (DB > env >
    # varsayilan) boylece kendiliginden dogar. Gecersiz bir deger burada
    # ValidationError uretir ve kosu HIC baslamaz -- sessiz geri dusus
    # operatorun niyetini yok sayardi (CFG S7).
    return settings_from_overrides(overrides)


@dataclass(frozen=True)
class FieldSchema:
    """Yonetim panelinin formu cizmek icin ihtiyac duydugu HER SEY.

    `min` / `max` ELLE YAZILMAZ: `Field` kisitlarindan (`Ge`, `Le`, `Gt`,
    `Lt`) turetilir. Kopyalansaydi `ge=1` bir gun `ge=2` olur ve panel
    bayat bir araligi dogrulardi.
    """

    key: str
    group: str
    type: str
    default: Any
    min: float | None
    max: float | None
    description: str


def _bounds(metadata: list[Any]) -> tuple[float | None, float | None]:
    """`Field` kisitlarindan (min, max).

    `gt` / `lt` de min/max sayilir: panel icin bilgi degeri aynidir
    (`gt=0` -> "0'dan buyuk"), asil dogrulama zaten pydantic'te kalir.
    """
    low: float | None = None
    high: float | None = None
    for item in metadata:
        for attr in ("ge", "gt"):
            value = getattr(item, attr, None)
            if value is not None:
                low = float(value)
        for attr in ("le", "lt"):
            value = getattr(item, attr, None)
            if value is not None:
                high = float(value)
    return low, high


def settings_schema() -> list[FieldSchema]:
    """DB-yonetimli 39 alanin makine-okunur semasi. SAF: DB'ye BAKMAZ.

    Sema ile DURUM bilincli olarak ayrildi (CFG S2/S6.4): sema surec omru
    boyunca sabittir, `value` her okumada degisebilir. Birlesik olsaydi
    saf sema agsiz test edilemez ve onbelleklenemezdi.
    """
    out: list[FieldSchema] = []
    for key in sorted(DB_MANAGED_FIELDS):
        info = Settings.model_fields[key]
        extra = info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}
        low, high = _bounds(list(info.metadata))
        annotation = info.annotation
        out.append(
            FieldSchema(
                key=key,
                group=str(extra.get("group", "")),
                type=getattr(annotation, "__name__", str(annotation)),
                default=info.default,
                min=low,
                max=high,
                description=info.description or "",
            )
        )
    return out


def settings_from_overrides(overrides: Mapping[str, str]) -> Settings:
    """Ham METIN ezmelerinden `Settings` kurar.

    `type: ignore` ZORUNLUDUR ve gecici degildir: alanlar `int` / `float`
    / `bool` olarak TIPLENMISTIR, oysa hem `.env` hem `settings` tablosu
    her degeri METIN verir ve donusumu pydantic yapar. Bu tek kapiya
    toplanmasi, susturmanin kod tabanina yayilmasini onler.
    """
    return Settings(**overrides)  # type: ignore[arg-type]


def get_settings() -> Settings:
    global _settings
    if _settings is None:  # hizli yol, kilitsiz
        with _lock:
            if _settings is None:  # double-checked
                _settings = _load()
    return _settings


def applied_overrides() -> dict[str, str]:
    """Yukleyicinin UYGULADIGI ham ezmeler (kopya).

    `settings` tablosu yeniden OKUNMAZ: kosan bir sync'in tutarli TEK bir
    anlik goruntu kullanmasi, shard'lar arasi yapilandirma carpikligini
    imkansiz kilar (CFG S3.5).
    """
    return dict(_overrides)


def install_settings(settings: Settings, overrides: Mapping[str, str] | None = None) -> None:
    """Cozulmus `Settings`i surece KURAR; yukleyici bir daha kosmaz.

    `overrides` de birlikte kurulur. Kurulmasaydi child'da
    `applied_overrides()` BOS donerdi -- bugun kimse cagirmiyor, ama
    "singleton kuruldu ama ezmeler bos" hali sessizce yanlis bir cevap
    uretirdi; iki degeri ayni kapidan gecirmek bu tuzagi hic kurmaz.

    Shard child'lari icindir (CFG S3.5): parent'in cozdugu deger
    `ShardSpec` ile tasinir ve child DB'ye HIC bakmaz. Child kendi
    `get_settings()`ini cagirsaydi araya giren bir `yfin config set`
    shard-0 ile shard-3'un FARKLI yapilandirmayla kosmasina yol acardi;
    ustelik child `settings.db_name`e baglanip asil isini
    `spec.database`de yapar -- yani YONLENDIRILMEDIGI semadan ayar
    okurdu.
    """
    global _settings, _overrides
    with _lock:
        _settings = settings
        _overrides = dict(overrides or {})


def reset_settings() -> None:
    """Singleton'i temizler (testler ve `yfin config` yazma yolu icin).

    Sifirlama OLMASAYDI repo testleri onceki testten dolu gelen bir
    singleton'la kosar, `load_overrides` HIC cagrilmaz ve testler YANLIS
    NEDENLE yesil kalirdi (CFG S8.2).
    """
    global _settings, _overrides
    with _lock:
        _settings = None
        _overrides = {}
