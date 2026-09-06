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

    yf_rate_limit_per_sec: float = Field(default=2.0, gt=0)
    yf_max_workers: int = Field(default=4, ge=1)
    yf_queue_maxsize: int = Field(default=8, ge=1)
    yf_retry_attempts: int = Field(default=5, ge=1)
    yf_retry_initial_sec: float = Field(default=1.0, ge=0)
    yf_retry_max_sec: float = Field(default=16.0, ge=0)
    yf_news_count: int = Field(default=50, ge=1)
    yf_news_tab: str = "all"
    yf_incremental_overlap_days: int = Field(default=7, ge=0)
    yf_delist_threshold: int = Field(default=5, ge=1)

    # --- shard'li calistirma (proxy havuzu) --------------------------------
    yf_max_shards: int = Field(default=4, ge=1)
    yf_shard_timeout_seconds: int = Field(default=3600, ge=60)
    yf_txn_retry_attempts: int = Field(default=3, ge=1)

    # --- proxy politikasi -------------------------------------------------
    yf_proxy_cooldown_seconds: int = Field(default=900, ge=1)
    yf_proxy_failure_threshold: int = Field(default=3, ge=1)
    yf_proxy_dead_rounds: int = Field(default=3, ge=1)
    yf_proxy_check_timeout: float = Field(default=10.0, gt=0)
    # Fernet anahtari; bossa parolali proxy EKLENEMEZ (sifre koda/DB'ye
    # duz metin girmez)
    yf_proxy_secret_key: str = ""

    # --- yfinance advanced ------------------------------------------------
    # yfinance'in tz/cookie/ISIN cache'i SQLite'tir; her shard kendi
    # dizinini alir, aksi halde ikinci yazar SQLITE_BUSY yer
    yf_tz_cache_dir: str = ".cache/yfinance"
    yf_history_repair: bool = True

    # Financials / market (S11)
    yf_earnings_dates_max_pages: int = Field(default=3, ge=1)
    # esgScores 19 sembolun 19'unda da 404 verdi; izleme dataset'i
    # VARSAYILAN KAPALIDIR (AH S6.3). Acilirsa her sembolde bir bosa istek
    # ve bir `empty` hucre uretir.
    yf_probe_sustainability: bool = False
    yf_market_regions: str = "US,EUROPE,ASIA,GB,CURRENCIES,CRYPTOCURRENCIES,COMMODITIES,RATES"
    yf_calendar_lookback_days: int = Field(default=7, ge=0)
    yf_calendar_lookahead_days: int = Field(default=30, ge=0)
    yf_calendar_page_limit: int = Field(default=100, ge=1, le=100)
    yf_calendar_max_pages: int = Field(default=5, ge=1)

    # --- sektor / endustri (SI S12) ---------------------------------------
    # ISO 3166-1 alpha-2, virgullu. ILKI BIRINCILDIR: bolgesiz dataset'ler
    # onun yanitini kullanir ve dogrulama probu onu taban alir.
    #
    # IKI AYAR, UC DEGIL. Ilk taslakta bir `yf_domain_include_reports`
    # bayragi vardi; cikarildi cunku hicbir yerde okunmuyordu ve okunsaydi
    # profil dataset'inin `produces`'ini KOSULLU yapar, denetimin hucre
    # sayisini yapilandirmaya bagimli kilardi. Rapor istemeyen kullanici
    # `--datasets sector_rankings,industry_rankings` diyebilir.
    yf_domain_regions: str = "US"
    # Bolge dogrulama probunun referans sektoru (SI S6.6)
    yf_domain_reference_sector: str = "technology"

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
    yf_search_max_results: int = Field(default=10, ge=1)
    yf_search_news_count: int = Field(default=5, ge=0)
    yf_search_lists_count: int = Field(default=10, ge=0)
    # `all` cagrisinin belge tavani ~1.000 olculdu; 1000 istemek tavana
    # kadarini alir. 250 istenseydi DAR terimlerde bile kirpardi
    # (BTC: count=250 -> 248 belge, oysa total 503).
    yf_lookup_count: int = Field(default=1000, ge=1)
    # `lookupTotals.all` bu degeri asarsa `all` cagrisi KIRPILMIS demektir
    # ve yedi tipli dala gecilir (SQ K6). Olculdu: BTC 503 -> `all` tam
    # kume; GOLD 7.273 -> `all` yalniz 995 belge, tipli birlesim 3.313.
    # Esik `all`in gozlenen tavaninin (~1.000) ALTINDA tutulur ki kirpilma
    # BASLAMADAN tipli dala gecilsin.
    yf_lookup_all_threshold: int = Field(default=500, ge=1)
    # Yahoo tavani 250; asilirsa `yf.screen` ValueError firlatir. le=250
    # sayesinde bu bir YAPILANDIRMA hatasi olur ve `Settings` yuklenirken
    # gorulur, kosu ortasinda degil.
    yf_screen_size: int = Field(default=250, ge=1, le=250)
    # Ekran basina sayfa ust siniri. 19 predefined: sinirsiz 49 istek,
    # cap=4 ile 32. En pahali ekran `most_shorted_stocks` (total 4.022,
    # 17 sayfa). Sinira takilan ekran `screen_runs.total` ile
    # `fetched_rows` farkindan GORULUR (SQ S9.6/1).
    yf_screen_max_pages: int = Field(default=4, ge=1)
    # Bos = `screens.py`deki tum ekranlar (DB'de kapatilmis olanlar haric).
    yf_screen_keys: str = ""

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
    yf_bar_prepost: bool = True
    # Artimli pencerede geriye ortusme. Ortusme idempotenttir (olculdu:
    # 78 ortak barda deger farki yok); maliyeti birkac yuz gereksiz
    # upsert, faydasi seans sinirindaki barin kacmamasi.
    yf_bar_overlap_days: int = Field(default=2, ge=0)

    # Budama VARSAYILAN KAPALI (S7.4): takvim ve _history satirlari yeniden
    # cekilemez, bu yuzden silme acikca etkinlestirilmeden calismaz.
    yf_prune_enabled: bool = False

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


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
