# DB Tabanli Yapilandirma (`settings` tablosu) — Tasarim Dokumani

- **Tarih:** 2026-09-05 (v2 — uc bagimsiz incelemeden sonra duzeltildi, bkz. Ek A)
- **Durum:** Uygulanmayi bekliyor
- **Proje koku:** `~/Projects/learn/yfinance/`
- **Kisaltma:** Bu dokumanin bolumleri metinde `S<X.Y>`, docstring'lerde `CFG§X.Y`
- **Onceki dokumanlar:** **T**, **F**, **P**, **AH**, **PB**, **SI**, **SQ**, **PG**
- **Kapsam:** `Settings` alanlarinin `.env`den veritabanina tasinmasi ve ileride
  bir yonetim arayuzunun okuyup yazabilecegi bir yapilandirma katmani

> **PG ile iliski.** `2026-09-04-postgres-timescaledb-migration-design.md` (**PG**)
> onaylanmis ve uygulanmayi bekliyor; motora ait tum kararlari (tip, collation,
> partition, kilit, upsert mekanigi) o dokuman belirler. Bu spec **motordan
> bagimsiz** yazilmistir (S2 "motor notrlugu" karari): kullandigi yardimcilar
> (`AsciiKeyType`, `TsType`, `Text`) PG donusum tablosunda **ayni adla** yasar,
> yalnizca govdeleri degisir. Bu spec PG'den once de sonra da uygulanabilir;
> yalnizca `Base.metadata`ya bir tablo ekler ve PG gocunun tablo sayimi 62'den
> 63'e cikar.

---

## 0. Onkosullar

| Onkosul | Nerede | Dogrulandi |
|---|---|---|
| `Settings` (pydantic-settings), `get_settings()` singleton'i | `config.py` | evet |
| `create_db_engine(settings, database, *, pool_size)` | `db.py:15` | evet |
| Shard child'i: `get_settings()` -> `configure_logging` -> `create_db_engine` | `shard.py:124`, `:130`, `:135` | evet |
| `AsciiKeyType(n)`, `TsType()` (PG'de de ayni ad) | `models/base.py:108`, `:161`; PG donusum tablosu | evet |
| `symbols` zaman damgasi deseni (`server_default` / `server_onupdate` now(6)) | `models/symbols.py:55`, `:58` | evet |
| Dongusel importta YEREL import deseni | `prune.py:94`, `:134`, `:269`, `:346`, `:441` | evet |
| Cikis kodu 2 = yapilandirma reddi | `cli.py:567-569` (`PruneDisabledError`) | evet |
| `register(..., opt_in=True)` deseni | `config.py:79-88`de gerekcelendirilmis | evet |
| Test semasi surece ozeldir | `tests/helpers.py:20 schema_name` | evet |
| Spawn edilen child `os.environ`i miras alir | `shard.py:380` `mp.get_context("spawn")` | evet |
| Pydantic: init kwargs env'i **ezer**; `extra="ignore"` bilinmeyen kwarg'i **sessizce yutar** | pydantic 2.13.5 / pydantic-settings 2.15.0, canli deney | evet |
| `Field` kisitlarindan min/max turetilebilir (`model_fields[k].metadata`) | canli deney: `yf_max_shards -> [Ge(1)]` | evet |

Bu tipler ve sozlesmeler **yeniden tanimlanmaz**.

---

## 1. Amac

Yapilandirmayi `.env` dosyalarindan alip **veritabanina** tasimak; boylece
ileride bir yonetim arayuzu ayarlari okuyup yazabilsin.

Kullanicinin belirttigi surucu **tek** bir seydir: *"ileride arayuzden
yonetmek"*. "Yeniden baslatmadan degistirmek", "cok makineyi tek merkezden
yonetmek" ve "degisiklik gecmisini denetlemek" **secilmedi**; bu tasarim
onlari cozmez ve cozuyormus gibi de yapmaz (S12).

Arayuz gereksiniminin iki somut sonucu var:

1. Panelin formu cizebilmesi icin **metadata** (tip, varsayilan, aralik,
   aciklama, grup) makine-okunur bicimde sunulmali (S6.4).
2. Panelden yazilan deger **yazim aninda** reddedilmeli; hata bir sonraki
   gece cron'unda cikmamali (S6.3).

### Kapsam ici

- `settings` tablosu (1 tablo, 4 kolon)
- `Settings`in **39 alani** icin DB ezmesi; **8 alan** `.env`de kalir
- `config/settings.seed.json` — kuruluma ait, depoya giren yapilandirma
- `yfin config` komut grubu: `list`, `get`, `set`, `unset`, `seed`, `export`, `schema`
- 39 alana `description` + `group` metadatasi
- **`yf_probe_sustainability`in `register(..., opt_in=True)` desenine tasinmasi**
  (S3.4 — zorunlu, bu tasarimin dogru calismasi buna bagli)

### Kapsam disi — gerekceli

| Cikarilan | Gerekce |
|---|---|
| `db_*` alanlarinin tasinmasi (6) | Bootstrap paradoksu: baglantiyi acan deger baglantinin ardinda duramaz |
| `yf_proxy_secret_key`in tasinmasi | Fernet anahtarini, sifreledigi proxy parolalariyla ayni yere koymak `proxy/crypto.py`yi anlamsizlastirir |
| `log_level`in tasinmasi | `configure_logging()` `create_db_engine()`den ONCE cagriliyor (`shard.py:130` vs `:135`); ayrica S3.3'un log sirasi bu alanin env'de olmasina DAYANIR |
| Degisiklik gecmisi tablosu (`settings_history`) ve `updated_by` kolonu | Denetim gereksinimi SECILMEDI. `updated_at` "en son ne zaman degisti"yi verir. Sonradan eklenmeleri bu tabloyu degistirmez; `updated_by` icin ayrica bir kimlik kaynagi (panel oturumu) gerekir ve o kaynak henuz yok |
| Esszamanli yazimda catisma tespiti | Son yazan kazanir (S2). `Settings`te alanlar arasi validator YOKTUR (hepsi bagimsiz `Field` kisiti), bu yuzden kaybedilen bir guncelleme yanlis bir "gecerli" kararina yol acamaz |
| TTL ile tazeleme / calisan kosuda etkili olma | "Yeniden baslatmadan degistirmek" SECILMEDI |
| Kosunun config surumunu `sync_runs`a yazmak | Denetim gereksinimi secilmedi |
| Metadatanin (tip / varsayilan / aralik) TABLOYA yazilmasi | S2; ikinci dogruluk kaynagi |
| Pydantic'in `settings_customise_sources` mekanizmasi | S2 karar tablosu; tavuk-yumurta |

---

## 2. Kararlar ve gerekceleri

| Karar | Secim | Gerekce |
|---|---|---|
| Motor notrlugu | Tablo yalniz `AsciiKeyType` / `Text` / `TsType` kullanir; motora ozgu hicbir sey YOK | **PG** gocu onaylanmis ve bekliyor; `MYSQL_TABLE_ARGS` orada TAMAMEN kaldiriliyor, `ascii_bin` -> `COLLATE "C"` oluyor. Bu spec o karara yaslanmaz, yardimcilarin ADINA yaslanir — onlar iki motorda da yasiyor |
| Tablo varligi denetimi | `sqlalchemy.inspect(engine).has_table("settings")` | Hata koduna bakmaktan (`1146` / `42P01`) hem daha saglam hem motordan bagimsiz. Ilk taslak `1146` yakaliyordu; PG'de sessizce yanlis olurdu |
| Depolama | Tek `settings` tablosu, `setting_key` + `value` | Metin `value`, `.env`in verdiginin AYNISI; ayni pydantic kod yolundan gecer, tip ve kisit tek yerde kalir |
| Kolon adi | `setting_key`, **`key` DEGIL** | `KEY` MySQL 8'de ayrilmis sozcuk (PG'de degil, ama motor notrlugu bunu da kapsar). Kod tabani bu tuzagi iki kez belgelemis: `models/base.py:69-78` (`interval` -> `bar_interval`), `models/funds.py:172` (`rank` -> `holding_rank`) |
| Kanonik anahtar bicimi | **Model alan adi** (kucuk harf). CLI girdiyi `strip().lower()` ile normalize eder | Operator `.env` aliskanligiyla `YF_MAX_SHARDS` yazacaktir; reddetmek gereksiz surtunmedir. Collation'in duyarli olmasinin amaci CAKISMAYI onlemek degil, ham SQL ile sokulmus `YF_MAX_SHARDS` satirinin AYRI ve **gorunur** kalmasi — boylece S3.2'nin "bilinmeyen anahtar" uyarisina takilir |
| Metadata nerede | **`Settings` modelinde**, tabloda DEGIL | Kopyalansaydi `Field(ge=1)` bir gun `ge=2` olur ve tablodaki `min` bayatlardi. `config.py`nin `YF_BAR_INTERVALS`i kaldirma gerekcesinin aynisi |
| Sema ile durumun ayrilmasi | `settings_schema()` (saf, DB'siz) + `settings_state()` (DB okur) | Ilk taslak ikisini tek nesnede birlestiriyordu: sema surec omru boyunca sabit, `value` her okumada degisebilir. Birlesik olsaydi saf sema onbelleklenemez ve agsiz test edilemezdi |
| Cozum sirasi | CLI bayragi > DB > `.env` > model varsayilani | Pydantic'te init kwargs env'i ezer (canli dogrulandi); ayri bir precedence mantigi YAZILMAZ |
| DB katmaninin baglanma bicimi | **Acik yukleyici** (`get_settings()` iki asamali) | `settings_customise_sources` tavuk-yumurta uretir ve `Settings(...)`i **13** test noktasinda DB'ye bagimli kilardi |
| `Settings` sinifi | **Degismez** (yalniz `Field` metadatasi eklenir) | **48** `get_settings()` cagri noktasi, 13 dogrudan `Settings(...)` kurulumu ve `create_db_engine` imzasi aynen kalir |
| Okuma ani | **Surec basinda bir kez**; shard'larda **parent'in cozdugu deger** | Kosan bir sync tutarli tek bir anlik goruntu kullanir. Her erisimde okumak sembol dongusu icinde (`history.py:145`, `bars.py:504`, `news.py:91`) on binlerce SELECT uretirdi |
| Shard'lara aktarim | Parent cozer, `ShardSpec` tasir; child **yeniden okumaz** | Uc sorunu birden kapatir: (a) child'lar arasi yapilandirma carpikligi, (b) N ekstra baglanti, (c) child'in `settings.db_name`e baglanip asil isini `spec.database`de yapmasi (`shard.py:135`) |
| Yukleyicinin engine'i | Kendi `create_engine(..., poolclass=NullPool, connect_args={"connect_timeout": 5})` | `create_db_engine` havuz boyutlandirmasi ve `pool_pre_ping` tek bir SELECT icin olu agirlik. Daha onemlisi: argumansiz cagrilirsa (`db.py:28` `settings or get_settings()`) **RecursionError**. `connect_timeout` olmadan DB erisilemezken CLI onlarca saniye asili kalir. Depo bu deseni zaten kullaniyor: `migrations/env.py:56` `poolclass=pool.NullPool` |
| `get_settings()` esszamanliligi | `threading.Lock` (double-checked) | Bugun kilitsiz ve zararsiz (~1 ms). DB okumasiyla birlikte iki worker thread iki ayri `Settings` NESNESI uretebilir; `test_client.py:88` gibi nesne kimligine dayanan yamalar sessizce etkisizlesirdi |
| "Ezme yok"un temsili | **Satirin olmamasi** | Bos dize mesru bir degerdir (`yf_news_tab=""`); NULL'u "ezme yok" saymak o degeri temsil edilemez kilardi |
| Gecersiz deger | Yazimda reddet, **okumada gurultulu cok** | Sessiz geri dusus operatorun niyetini yok sayar ve bunu yalniz log'a bakan fark eder |
| Dogrulama + yazmanin yeri | `settings_store.set_setting()` **saf fonksiyon**; CLI ince sarmalayici | Dogrulama CLI komutunun icine gomulseydi gelecek panel onu ATLAR ve ham SQL'e duserdi — ki S7 bunu felaket senaryosu sayiyor |
| Tohum kaynagi | `config/settings.seed.json` | Kullanici karari: degerler `.env`de degil JSON'da dursun |
| `seed`in kapsami | **Yalniz JSON'daki anahtarlar** (+ `--adopt-env` ile bir kereligine env devralma) | Ilk taslak `seed`i "tum eksik satirlari doldur" olarak tanimliyordu; bu, `unset`i SESSIZCE geri alirdi (iki komut birbirinin isini bozardi). JSON'a bagli kapsam `unset`i kalici kilar |
| Esszamanli yazim | Son yazan kazanir | S1 kapsam disi tablosu |

---

## 3. Mimari

### 3.1 Alan bolunmesi ve yukleyici

```python
# config.py
ENV_ONLY_FIELDS = frozenset({
    "db_host", "db_port", "db_user", "db_password", "db_name", "db_test_name",
    "yf_proxy_secret_key",   # sifreledigi metinle ayni yerde duramaz
    "log_level",             # configure_logging, create_db_engine'den ONCE
})

# TAMLAYAN olarak turetilir: Settings'e yeni bir alan eklendiginde
# kendiliginden DB-yonetimli olur. S8.1'deki DORT cit bunun guvenli
# kalmasini saglar (tuketicilik, metadata, SIR ADI kalibi, skalerlik).
DB_MANAGED_FIELDS = frozenset(Settings.model_fields) - ENV_ONLY_FIELDS   # 39
```

```python
# config.py
_lock = threading.Lock()

def _load() -> Settings:
    env_only = Settings()                       # env + varsayilan; db_* burada dolar
    # log_level ENV_ONLY oldugu icin BURADA hazirdir; yukleyicinin
    # uyarilari yapilandirilmis logger'a duser (S3.3).
    configure_logging(env_only.log_level)
    if _source_is_env():
        return env_only
    overrides = load_overrides(env_only)         # settings_store.py
    return Settings(**overrides) if overrides else env_only


def get_settings() -> Settings:
    global _settings
    if _settings is None:                        # hizli yol, kilitsiz
        with _lock:
            if _settings is None:                # double-checked
                _settings = _load()
    return _settings


def _source_is_env() -> bool:
    """`YF_SETTINGS_SOURCE=env` -> DB katmani HIC okunmaz.

    Deger `strip().lower()` ile karsilastirilir; `env` disindaki bos
    olmayan her deger WARNING uretir. Kurtarma amacli bir anahtarin bir
    yazim hatasi yuzunden SESSIZCE etkisiz kalmasi kabul edilemez.
    """
```

`Settings(**overrides)` — init kwargs pydantic'te env'i ezdigi icin cozum
sirasi **DB > env > varsayilan** olarak kendiliginden dogar.

### 3.2 `settings_store.load_overrides(settings)`

Bootstrap `Settings`i **parametre alir** — bu bir test kolayligi degil,
sozlesmenin parcasidir: repo testleri onu test semasina yoneltebilsin ve
shard parent'i cozdugu degeri child'a gecirebilsin diye.

```python
def load_overrides(settings: Settings) -> dict[str, str]:
    engine = create_engine(                      # create_db_engine DEGIL (S2)
        settings.db_url(), poolclass=NullPool,
        connect_args={"connect_timeout": 5},
    )
    try:
        if not sqlalchemy.inspect(engine).has_table("settings"):
            log.info("settings tablosu yok; yalniz env kullaniliyor")
            return {}
        ...
    finally:
        engine.dispose()
```

**Uc filtre**, ucu de sessiz gecmez:

| Durum | Davranis | Neden zorunlu |
|---|---|---|
| Model alani olmayan anahtar | `WARNING` + yok sayilir | `Settings` `extra="ignore"` tasiyor ve bilinmeyen kwarg'i **sessizce yutuyor** (canli dogrulandi). Bu filtre bir "iyi olur" degil, **zorunluluktur**; atlanirsa hicbir test kirmiziya donmez |
| `ENV_ONLY_FIELDS` anahtari | `WARNING` + **reddedilir** | **Guvenlik siniri.** Uygulanabilseydi bir DB satiri `db_host`u degistirip baglantiyi baska yere cevirebilir ya da `yf_proxy_secret_key`i ezebilirdi |
| Tablo yok | `INFO` + env-only devam | `yfin db upgrade` komutunun KENDISI `get_settings()` cagiriyor (`cli.py:191`, `migrations/env.py:36`) ve tablo henuz yok |

**Dorduncu durum — gecersiz deger — burada YAKALANMAZ.** `load_overrides`
ham metin dondurur; hata `_load` icindeki `Settings(**overrides)` cagrisinda
`ValidationError` olarak dogar (S7). Boylece dogrulama tek yerde kalir.

### 3.3 Log sirasi

Yukleyicinin uyarilari (guvenlik siniri dahil) `configure_logging()`den
**once** basilsaydi, `logging_setup.py:27`nin `cache_logger_on_first_use=True`
ayari yuzunden yapilandirilmamis `PrintLogger`a duserdi — `shard.py:125-128`
bu tuzagi zaten belgeliyor. `redact_credentials` processor'i da devrede
olmazdi.

Cozum ucuz: `log_level` **`ENV_ONLY_FIELDS`tedir**, yani `env_only`
kuruldugu anda hazirdir. `_load()` `configure_logging(env_only.log_level)`i
`load_overrides`tan ONCE cagirir. Mevcut cagri noktalari (`cli.py:170`,
`shard.py:130`) idempotent olduklari icin degismez.

### 3.4 `yf_probe_sustainability` -> `opt_in=True` (ZORUNLU)

`datasets/analysis/sustainability.py:53` bugun **modul govdesinde**
`get_settings()` cagiriyor:

```python
if get_settings().yf_probe_sustainability:
    register(SustainabilityDataset())
```

Kod tabanindaki **tek** modul-kapsamli `get_settings()` cagrisi budur (AST
taramasiyla dogrulandi) ve zincir su: `cli.py` -> `yfin.datasets` ->
`analysis` -> `sustainability`. Yani `yfin --help`, `yfin datasets`, hatta
`yfin config unset` bile Typer app'i kurulmadan once `get_settings()`i
tetikler.

DB katmani eklendiginde bunun uc sonucu olur:

1. **`log_level`i kapsam disi tutma gerekcesi cokerdi** — DB'ye hic
   dokunmayan komutlar yine de baglanirdi.
2. **DB kapaliyken CLI tumuyle kullanilamaz hale gelirdi**, kurtarma
   komutlari dahil.
3. **Test izolasyonu kurulamazdi** — pytest once modulleri import eder,
   fixture'lar sonra calisir (S8.3).

Cozum `config.py:79-88`de **zaten yazili**: `yf_discovery_enabled` ayni
tuzaga dusmustu ve kaldirilip `register(..., opt_in=True)` desenine
gecilmisti. `sustainability` o gocte atlanmis. Bu spec onu bitirir:

```python
register(SustainabilityDataset(), opt_in=True)   # kosul YOK
```

`yf_probe_sustainability` boylece registry'den tamamen ayrilir ve
DB-yonetimli kalabilir. Dataset `--datasets sustainability` ile adiyla
istendiginde kosar, `all` genislemesinde gorunmez.

**S8.1'e bir cit eklenir:** `ast` ile modul govdesinde `get_settings()`
cagrisi taranir; bulunursa test patlar. Bu tuzagin ucuncu kez kurulmasini
onler.

### 3.5 Okuma sayisi ve shard'lar

```
yfin sync
  |- ana surec  get_settings() -> 1 SELECT, overrides cozulur
  |- ShardSpec(..., settings_overrides={...})              <-- parent'in cozdugu deger
  |- shard-0 (spawn) Settings(**spec.settings_overrides)   -> 0 SELECT
  '- shard-1 (spawn) Settings(**spec.settings_overrides)   -> 0 SELECT
```

Ilk taslak "`shard.py`ye dokunulmaz" diyordu; bu bir kazanc degil maliyetti.
Child'lar kendi `get_settings()`lerini cagirdiginda (`shard.py:124`) araya
giren bir `yfin config set` shard-0 ile shard-3'un **farkli yapilandirmayla**
kosmasina yol acar; ayrica child `settings.db_name`e baglanirken asil isini
`spec.database`de yapar (`shard.py:135`) — `--database` ile yonlendirilmis
bir kosuda **yonlendirilmedigi** semadan ayar okur.

`ShardSpec` zaten saf veri dataclass'idir; `settings_overrides: dict[str, str]`
alani eklenir ve `shard_main` `config._settings`i ondan doldurur.

---

## 4. Sema

### 4.1 `settings`

| Kolon | Tip | Kisit / not |
|---|---|---|
| `setting_key` | `AsciiKeyType(64)` | **PK**. Buyuk/kucuk harf duyarli collation: amac cakismayi onlemek DEGIL, kanonik olmayan bir anahtarin (`YF_MAX_SHARDS`) AYRI ve **gorunur** kalip S3.2'nin uyarisina takilmasi |
| `value` | `Text` | **NOT NULL**. Bos dize mesru bir degerdir; "ezme yok" demek satirin OLMAMASIDIR |
| `created_at` | `TsType()` | `server_default=func.now(6)` |
| `updated_at` | `TsType()` | `server_default=func.now(6)`, `server_onupdate=func.now(6)` — `models/symbols.py:55,58` deseni |

Motora ozgu tablo argumani **kullanilmaz** (S2 motor notrlugu). `Base`in
mevcut varsayilanlari gecerlidir; PG gocu bu tabloyu da diger 62'siyle ayni
kuralla donusturur.

**Tabloda olmayanlar:** tip / varsayilan / min / max / aciklama kolonlari
(S2), FK, gecmis tablosu, `updated_by`, `is_secret` bayragi (S1).

**Satir sayisi bir DEGISMEZ DEGILDIR.** Tohumlamadan hemen sonra JSON'daki
anahtar sayisi kadardir; `set` artirir, `unset` azaltir. Satirin olmamasi
mesrudur (S5.4).

### 4.2 Alan bolunmesi

| Kume | Adet | Icerik |
|---|---|---|
| `ENV_ONLY_FIELDS` | 8 | `db_host`, `db_port`, `db_user`, `db_password`, `db_name`, `db_test_name`, `yf_proxy_secret_key`, `log_level` |
| `DB_MANAGED_FIELDS` | 39 | Kalan her sey |
| **Toplam** | **47** | `Settings.model_fields` (2026-09-05 anlik goruntusu) |

`YF_SETTINGS_SOURCE` bir `Settings` alani **degildir** (`os.getenv` ile
okunur) ve bu sayima girmez; `.env`de yasayabilir.

### 4.3 Gruplar

| Grup | Adet | Alanlar |
|---|---|---|
| `client` | 4 | `yf_rate_limit_per_sec`, `yf_retry_attempts`, `yf_retry_initial_sec`, `yf_retry_max_sec` |
| `runner` | 3 | `yf_max_workers`, `yf_queue_maxsize`, `yf_txn_retry_attempts` |
| `shard` | 2 | `yf_max_shards`, `yf_shard_timeout_seconds` |
| `proxy` | 4 | `yf_proxy_cooldown_seconds`, `yf_proxy_failure_threshold`, `yf_proxy_dead_rounds`, `yf_proxy_check_timeout` |
| `symbols` | 2 | `yf_delist_threshold`, `yf_incremental_overlap_days` |
| `datasets` | 6 | `yf_news_count`, `yf_news_tab`, `yf_earnings_dates_max_pages`, `yf_probe_sustainability`, `yf_history_repair`, `yf_tz_cache_dir` |
| `market` | 5 | `yf_market_regions`, `yf_calendar_lookback_days`, `yf_calendar_lookahead_days`, `yf_calendar_page_limit`, `yf_calendar_max_pages` |
| `domain` | 2 | `yf_domain_regions`, `yf_domain_reference_sector` |
| `discovery` | 8 | `yf_search_max_results`, `yf_search_news_count`, `yf_search_lists_count`, `yf_lookup_count`, `yf_lookup_all_threshold`, `yf_screen_size`, `yf_screen_max_pages`, `yf_screen_keys` |
| `bars` | 2 | `yf_bar_prepost`, `yf_bar_overlap_days` |
| `maintenance` | 1 | `yf_prune_enabled` |
| **Toplam** | **39** | 11 grup |

> `yf_discovery_enabled` **YOKTUR**; `config.py:79-88` onun bilincli olarak
> kaldirildigini ve yerine `register(..., opt_in=True)` desenine gecildigini
> yaziyor. v1 bu alani yanlislikla listelemisti.

### 4.4 Deger serilestirme

39 alanin tamami **skalerdir** (`bool` / `float` / `int` / `str`; canli
dogrulandi). Kural:

| Tip | `value` sutununa | Geri okuma |
|---|---|---|
| `str` | oldugu gibi | pydantic |
| `int` / `float` | `str(v)` | pydantic |
| `bool` | `"true"` / `"false"` | pydantic |

Karmasik tip (liste / sozluk) icin kural **tanimlanmamistir**, cunku boyle bir
alan yoktur. `yf_market_regions` / `yf_domain_regions` / `yf_screen_keys`
`str`dir (virgullu), liste degil. S8.1'e bir cit eklenir: DB-yonetimli bir
alan skaler olmayan bir tipe donerse test patlar ve bu tablo guncellenmek
zorunda kalir.

**Gidis-donus garantisi:** `seed(export(state)) == state` her anahtar icin
saglanir; S8.2 bunu acikca test eder.

### 4.5 Migration

Tek revizyon: `<uygulama gunu>_<HHMM>_settings_table.py`. `down_revision`
**uygulama anindaki head**tir; bu dokuman yazilirken `f3b8c1d47e29`
(`20260905_0920_search_lookup_screener.py`), ama paralel isler head'i
kaydirdigi icin deger uygulama sirasinda `alembic heads` ile **dogrulanir**
(S10, Plan A adim 1'in kabul kriteri).

**upgrade:** `settings` tablosunu **BOS** olusturur. **downgrade:** dusurur.

Migration **veri tasimaz**. Kod tabaninin kendi deseni budur ve iki yerde
gerekcelendirilmistir: SI'de 156 domain satiri `domain_taxonomy` dataset'iyle
yazilir (`20260904_1730_...py:4-5`); SQ'da `screens` seed EDILMEZ
(`20260905_0920_...py:12-20`).

---

## 5. Tohum, uzlastirma ve `unset`

### 5.1 `config/settings.seed.json`

Depoya girer ve **bu kurulumun yapilandirmasidir**. Duz sozluk, yalniz DB'nin
yonettigi anahtarlar, S4.4'un serilestirme kuralina uygun:

```json
{
  "yf_max_shards": 8,
  "yf_domain_regions": "US,GB",
  "yf_prune_enabled": false
}
```

**Tam liste olmak zorunda degildir** ve olmamalidir: yalnizca varsayilandan
sapmak istediginiz anahtarlar yazilir. Dosyaya yazilmayan bir anahtarin degeri
model varsayilanindan gelir ve `Settings`teki varsayilan degistiginde kuruluma
**yansir** — tam liste yazilsaydi bu bag kopardi.

Sirlar giremez: `seed` `ENV_ONLY_FIELDS`ten bir anahtar gorurse hata verip
hicbir sey yazmaz. Bugunku 39 anahtarin hicbiri sir degildir; S8.1'deki
**sir-adi citi** bunun boyle kalmasini saglar.

### 5.2 `yfin config seed`

**Kapsam: yalnizca JSON'da bulunan anahtarlar.**

```
JSON'daki anahtarin satiri YOK   -> JSON degeri yazilir
JSON'daki anahtarin satiri VAR   -> DOKUNULMAZ   (--force ile JSON degeri yazilir)
JSON'da OLMAYAN anahtar          -> HICBIR SEY YAPILMAZ (--force dahil)
```

Son satir kritiktir: `--force` **yalnizca JSON'daki anahtarlarin** satirlarini
ezer. Aksi halde `--force`, operatorun panelden yaptigi tum ezmeleri sessizce
silerdi.

**Ya hep ya hic:** once JSON'un tamami modele dogrulatilir (bilinmeyen anahtar
/ gecersiz deger / env-only anahtar -> hata), sonra tek transaction'da
yazilir. `--dry-run` ne yazilacagini basar; eksik satir varsa CI'da
kullanilabilsin diye **cikis kodu 1** doner.

**`--adopt-env` (bir kereligine, goc icin).** JSON kapsaminin disinda kalan,
satiri OLMAYAN her anahtar icin o anki **etkin** degeri (`.env` -> varsayilan)
yazar. Etkin deger, DB katmani devre disi birakilmis bir bootstrap `Settings()`
orneginden okunur — boylece tohumlama kendi yazdigi satirlari geri beslemez.

Gerekcesi S9'dadir: `.env`inde `YF_MAX_SHARDS=8` olan bir kurulum bu adim
olmadan migration sonrasi sessizce 4'e donerdi.

### 5.3 `unset` ile iliski

`unset KEY` satiri **siler**; anahtar `.env`e ve model varsayilanina duser.

v1'de `seed` "tum eksik satirlari doldur" idi ve bu `unset`i SESSIZCE geri
alirdi: iki komut birbirinin isini bozardi. JSON'a bagli kapsam bunu cozer:

- Anahtar JSON'da **degilse**: `unset` **kalicidir**. Sonraki `seed` ona
  dokunmaz.
- Anahtar JSON'da **ise**: sonraki `seed` onu geri koyar — ve bu **dogru**
  davranistir, cunku JSON "bu kurulumun yapilandirmasi"dir. `unset` ciktisi
  bunu uyarir: *"`yf_max_shards` seed dosyasinda tanimli; bir sonraki `yfin
  config seed` onu geri koyacak."*

### 5.4 Eksik satir olumcul degildir — ama sessiz de degildir

Satiri olmayan anahtar cozum zincirinde `.env`e, o da yoksa model
varsayilanina duser. **Ancak S9 adim 5'ten sonra `.env` katmani fiilen
bostur**, yani sonradan `Settings`e eklenen bir alan icin dusus dogrudan model
varsayilaninadir. Bu, S2'nin `--adopt-env` ile korumaya calistigi senaryonun
ta kendisidir — sadece goc aninda degil, **her yeni alanda** yeniden dogar.

Bu yuzden:

- `yfin config list` satiri olmayan anahtarlari **isaretler**;
- `yfin db upgrade` eksik satir varsa bir satirlik uyari basar (komut
  `Settings.model_fields`i okur; bu bir migration degil bir KOMUT oldugu icin
  S4.5'in "migration uygulama kodunu import etmesin" ilkesini ihlal etmez);
- `yfin config seed --dry-run` cikis kodu 1 ile CI'da kullanilabilir.

**Panelin eksiksizligi tohumlamaya BAGLI DEGILDIR:** panel `settings_schema()`
+ `settings_state()` birlesimini okur ve 39 anahtarin tamamini satiri olsun
olmasin gorur (S6.4). v1 tohumlamanin gerekcesini "panelin eksiksizligi" diye
yazmisti; bu yanlisti. Tohumlamanin gercek gerekcesi **`.env` bagimliligini
sonlandirmak ve goc sonrasi davranisi sabitlemektir**.

---

## 6. CLI ve panel yuzeyi

### 6.1 Tam cozum sirasi

```
CLI bayragi (--regions, --shards)  >  settings tablosu  >  .env  >  model varsayilani
```

`yfin domain sync --regions US,GB` bugunku `settings.model_copy(...)` desenini
korur (`cli.py:825`): DB'ye yazmaz, o kosuya ozeldir.

### 6.2 `yfin config`

| Komut | Davranis |
|---|---|
| `list [--group X] [--changed] [--source db\|env\|default]` | 39 anahtar, etkin deger ve kaynak. Satiri olmayanlari isaretler. `--changed` = **etkin degeri model varsayilanindan farkli olanlar** (operatorun sordugu soru budur); satir varligi icin `--source db` |
| `get KEY` | Tek deger + kaynagi |
| `set KEY VALUE` | Dogrular, satiri yazar |
| `unset KEY` | Satiri siler; artik hangi degerin gecerli olacagini ve JSON'da tanimliysa S5.3 uyarisini basar. Satir yoksa **cikis 0** ve bilgilendirme (idempotent) |
| `seed [--dry-run] [--force] [--adopt-env]` | S5.2 |
| `export [--all]` | **Her zaman JSON.** Varsayilan: yalnizca DB satiri olanlar (= seed dosyasinin dogal tersi). `--all`: 39 degerin tam anlik goruntusu |
| `schema [--json]` | Varsayilan tablo, `--json` panel bicimi |

`list` / `get` **yalniz 39 DB-yonetimli anahtari** gosterir. 8 env-only alan
panelden yonetilemez ve listede yer almaz; `yfin config schema` ciktisinin
basinda tek satirlik bir not bunu soyler.

`YF_SETTINGS_SOURCE=env` etkinken `list` / `get` basliga **"DB katmani
KAPALI"** uyarisi basar; `set` / `unset` / `seed` yine de **DB'ye yazar** —
kurtarma senaryosu tam olarak budur.

`config list` / `get` / `schema` DB erisilemezken **cokmez**: uyari basip
`source=env|default` ile devam eder. Kurtarma komutu, kurtarmaya calistigi
arizaya kurban gitmemelidir.

### 6.3 Dogrulama ve yazma

```python
# settings_store.py
def set_setting(key: str, value: str, *, settings: Settings) -> None:
    """Dogrular ve yazar. `yfin config set` bunun INCE bir sarmalayicisidir;
    gelecek panel AYNI fonksiyonu cagirir."""
```

Dogrulama, okumanin kullandigi kod yolunun **aynisidir**: deger
`Settings(**{**mevcut_ezmeler, key: value})` kurularak dogrulanir. Iki ayri
dogrulama yazilsaydi biri gevser ve panel "gecerli" dedigi degeri bir sonraki
kosuda coktururdu.

Uc durumda yazmaz ve **cikis kodu 2** doner (`cli.py:567-569` deseni):
bilinmeyen anahtar, `ENV_ONLY_FIELDS` anahtari, kisiti ihlal eden deger.

### 6.4 `settings_schema()` ve `settings_state()`

**Ikisi ayridir** (S2):

```python
settings_schema() -> list[FieldSchema]      # SAF, DB'siz, onbelleklenebilir
# {"key", "group", "type", "default", "min", "max", "description"}

settings_state(settings) -> dict[str, tuple[str, Source]]   # DB okur
# {"yf_max_shards": ("8", Source.DB)}
```

Panel ikisini birlestirir; `yfin config schema` safi, `yfin config list`
birlesimi basar.

`type` / `default` / `min` / `max` pydantic'in `model_fields[k].metadata`
alanindan okunur (`yf_max_shards -> [Ge(ge=1)]`, `yf_calendar_page_limit ->
[Ge(1), Le(100)]`; canli dogrulandi) — elle yazilmaz, dolayisiyla bayatlayamaz.

---

## 7. Hata davranisi

| Durum | Davranis | Gerekce |
|---|---|---|
| `settings` tablosu yok (`has_table` false) | `INFO` + env-only devam | `yfin db upgrade`in kendisi `get_settings()` cagiriyor |
| Veritabani yok / erisilemez | `yfin db create` ve `yfin db revision` DB katmanini **hic kullanmaz** (S10 Plan A/4); diger komutlarda hata **yukselir** | Veritabanini YARATAN komut, var olmayan veritabanina baglanamaz |
| Yetki yok (`settings` uzerinde okuma izni yok) | **Yukselir** | Sessiz env-only devam, yanlis yapilandirmayla kosmak demektir. S9'a izin adimi eklendi |
| Tabloda bilinmeyen anahtar | `WARNING` + yok sayilir | `extra="ignore"` sessizce yutar; filtre zorunlu (S3.2) |
| Tabloda env-only anahtar | `WARNING` + reddedilir | Guvenlik siniri |
| Tabloda gecersiz deger | `ValidationError` — **kosu hic baslamaz** | Sessiz geri dusus operatorun niyetini yok sayardi |
| `yfin config set` gecersiz deger | Yazilmaz, cikis kodu 2 | Geri bildirim yazim aninda |
| `yfin config list` DB erisilemez | Uyari + `env` / `default` ile devam | Kurtarma komutu arizaya kurban gitmemeli |

`YF_SETTINGS_SOURCE=env` bir hata durumu **degildir**; normal bir calisma
modudur ve S3.1'de tanimlidir.

---

## 8. Test stratejisi

### 8.1 `tests/unit/` — agsiz

| Dosya | Kapsam |
|---|---|
| `test_settings_split.py` | **Dort cit.** (1) Tuketicilik: `ENV_ONLY ∪ DB_MANAGED == model_fields`, kesisim BOS. (2) Metadata: DB-yonetimli HER alanin `description` + `group`u var, grup bilinen 11 kumeden. (3) **Sir-adi citi:** adi `secret\|password\|token\|credential` kalibina uyan yeni bir alan `ENV_ONLY_FIELDS`te degilse **patlar** — tamlayan turetme yeni alanlari varsayilan olarak DB'ye acar, bu fail-open'i kapatir. (4) **Skaler citi:** DB-yonetimli her alan `bool\|int\|float\|str` (S4.4) |
| `test_no_import_time_settings.py` | **`ast` citi:** `src/yfin` altinda modul govdesinde `get_settings()` cagrisi YOK. S3.4'teki tuzagin ucuncu kez kurulmasini onler |
| `test_settings_resolution.py` | Cozum sirasi: ezme > env > varsayilan. `YF_SETTINGS_SOURCE=env` iken `load_overrides` **hic cagrilmaz** (casus); tanimsiz deger WARNING uretir. Bilinmeyen anahtar -> WARNING + yok sayilir. `load_overrides` `get_settings()` **cagirmaz** (RecursionError citi) |
| `test_settings_security.py` | Tabloda `db_host` satiri varmis gibi davranildiginda `settings.db_host` **DEGISMEZ** + WARNING |
| `test_settings_schema.py` | `settings_schema()` **saf**: DB'siz calisir, 39 kayit, `min` / `max` `Field` kisitlarindan turuyor |
| `test_settings_seed_plan.py` | Tohum planlayicisi **saf fonksiyon**: JSON kapsami disina cikmaz; `--force` JSON disi satirlara dokunmaz; `--adopt-env` yalniz satirsiz anahtarlari doldurur ve etkin degeri **DB'siz** bootstrap'tan okur |

### 8.2 `tests/repo/` — gercek DB

| Dosya | Kapsam |
|---|---|
| `test_settings_schema_repo.py` | `setting_key` buyuk/kucuk harf duyarli; `value` NOT NULL |
| `test_settings_seed_repo.py` | Temiz tablo uzerinde `seed` JSON kadar satir yazar, ikinci kosu 0; `--force` yalniz JSON anahtarlarini ezer; gecersiz JSON'da **hicbir sey yazilmaz**; `unset` + `seed` sonrasi JSON disi anahtar **geri gelmez** (S5.3) |
| `test_settings_roundtrip_repo.py` | **`seed(export(state)) == state`** her anahtar icin; bool / int / float / str serilestirmesi kayipsiz (S4.4) |
| `test_settings_cli_repo.py` | `set` / `unset` / `get` gidis-donusu; gecersiz deger -> cikis 2, satir yazilmaz; `unset` var olmayan satirda cikis 0; `list --changed` tanimi |
| `test_settings_load_repo.py` | `load_overrides` gercek tabloyu okur; ham SQL ile sokulan bozuk deger `get_settings()`i **coktrur**; tablo yokken env-only devam eder |
| `test_settings_shard_repo.py` | `ShardSpec.settings_overrides` parent'tan child'a tasinir; child **SELECT atmaz** ve `spec.database` ile `db_name` farkliyken dogru degerleri kullanir |

`load_overrides(settings)` bootstrap `Settings`i parametre aldigi icin repo
testi onu **test semasina** yoneltebilir.

**Her repo testi `config._settings = None` sifirlamasi yapar.** Aksi halde
singleton onceki testlerden dolu gelir, `load_overrides` hic cagrilmaz ve test
**yanlis nedenle** yesil kalir.

### 8.3 `tests/conftest.py`

`YF_SETTINGS_SOURCE=env`, `conftest.py`nin **EN BASINDA, modul seviyesinde** ve
her `yfin` import'undan **once** kurulur:

```python
import os
os.environ.setdefault("YF_SETTINGS_SOURCE", "env")   # yfin importlarindan ONCE

from yfin.config import Settings, get_settings       # noqa: E402
```

**Autouse session fixture'i YETMEZ** ve v1'in onerisi yanlisti: pytest once tum
test modullerini **import eder**, fixture'lar ondan **sonra** calisir. Ayrica
session-scoped bir fixture function-scoped `monkeypatch`i isteyemez
(`ScopeMismatch`).

Bu zorunludur cunku `load_overrides` `db_name`e — yani **uretim semasina** —
baglanir, oysa testler surece ozel bir semada kosar (`tests/helpers.py:20`).
Spawn edilen child'lar `os.environ`i miras alir (`shard.py:380`), dolayisiyla
degisken onlara da gecer.

DB yolunu sinayan repo testleri `monkeypatch.delenv` **ve**
`config._settings = None` uygular.

---

## 9. Goc

1. **Izin:** DB kullanicisina `settings` tablosu uzerinde okuma (ve yazacaksa
   yazma) izni verilir. Kisitli bir kullaniciyla kosan kurulumda bu adim
   atlanirsa `alembic upgrade head` sonrasi **her komut** yetki hatasiyla
   coker (S7).
2. `alembic upgrade head` — `settings` tablosu (bos)
3. `yfin config seed --adopt-env` — mevcut `.env` **onurlandirilir**
4. `yfin config list` — `source` kolonunun `env`den `db`ye dondugu dogrulanir
5. O 39 anahtardan `.env`de fiilen bulunanlar silinir

**5. adimdan sonra `.env` katmani fiilen bostur.** `Settings`e sonradan eklenen
bir alan icin `yfin config seed` calistirilmazsa deger artik `.env`e degil
dogrudan model varsayilanina duser (S5.4). Bu yuzden **`yfin config seed` her
`alembic upgrade head` sonrasi standart adimdir.**

**Downgrade uyarisi:** 5. adimdan sonra yapilandirma yalnizca DB'de yasar.
Oncesinde tam anlik goruntu alin — ve **depoya degil**:

```
yfin config export --all > ~/yfinance-settings-backup.json
```

`config/settings.seed.json` kuruluma ozgu degerlerle kirletilmemelidir; o dosya
depo yapilandirmasidir (S5.1) ve `--all` ciktisi model varsayilanlarini da
icerdigi icin oraya yazilirsa varsayilan degisikliklerinin kuruluma yansima
bagi kopar.

`.env`de kalan: 8 anahtar (`db_*`, `yf_proxy_secret_key`, `log_level`) arti
`YF_SETTINGS_SOURCE` (bir `Settings` alani degildir).

---

## 10. Uygulama sirasi — IKI PLAN

Dokuman 1 tablo, 1 migration, 3 yeni modul, `get_settings()` yeniden yazimi,
`shard.py` degisikligi, `conftest` degisikligi, 7 CLI alt komutu, 39 alana
metadata ve 12 test dosyasi iceriyor. Dogal kirilma cizgisi **okuma yolu /
yazma yuzeyi**dir.

### Plan A — Okuma yolu (tek basina sevk edilebilir)

Tablo bos oldugu surece **davranis hic degismez**; riskin tamami buradadir.

0. **Citler once:** `test_settings_split.py` (dort cit) +
   `test_no_import_time_settings.py`
1. `models/settings.py` + migration (S4). **Kabul kriteri:** `alembic heads`
   ile `down_revision` dogrulanir
2. **`yf_probe_sustainability` -> `register(..., opt_in=True)`** (S3.4). Adim
   0'daki `ast` citi bu adimdan sonra yesile doner
3. `ENV_ONLY_FIELDS` / `DB_MANAGED_FIELDS` + 39 alana `description` + `group`
4. `settings_store.load_overrides()` + uc filtre + `NullPool` /
   `connect_timeout`; `yfin db create` ve `yfin db revision` DB katmanini atlar
5. `get_settings()` iki asamali + `threading.Lock` + `configure_logging` sirasi
   + `YF_SETTINGS_SOURCE`
6. `tests/conftest.py` modul seviyesi `os.environ.setdefault` — **5. adimla
   AYNI PR'da olmak zorunda**, aksi halde tum test kosusu uretim semasina
   baglanir
7. `ShardSpec.settings_overrides` + `shard_main` (S3.5)

**Birlestirilebilir en kucuk birim 1-7'dir**, 1-5 degil: adim 6 adim 5'siz test
kosusunu kirar, adim 7 adim 5'siz shard carpikligi birakir.

### Plan B — Yazma yuzeyi ve panel (A'ya baglidir)

8. `settings_schema()` (saf) + `settings_state()` (S6.4)
9. `settings_store.set_setting()` saf fonksiyonu (S6.3)
10. Tohum planlayicisi + `config/settings.seed.json` (S5)
11. `yfin config` komut grubu (S6.2)
12. Goc dokumantasyonu + `.env.example` guncellemesi (39 anahtar cikarilir,
    kalan 8 + `YF_SETTINGS_SOURCE` birakilir). Uygulama PR'inda `.env.example`
    yazma izni acilir

`unset` / `seed` etkilesimi (S5.3) ve panel yuzeyi tamamen Plan B'ye aittir ve
Plan A'yi bloke etmez.

---

## 11. Riskler

| Risk | Azaltma |
|---|---|
| Import aninda `get_settings()` -> CLI DB'siz kullanilamaz | S3.4 `opt_in=True` + S8.1 `ast` citi |
| Test kosusu uretim `settings` tablosunu okur | S8.3 **modul seviyesi** env kurulumu (session fixture YETMEZ) |
| Bozuk deger tum kosulari coktrur | `set` yazimda dogrular; `YF_SETTINGS_SOURCE=env` kurtarma yolu; `config list` DB'siz calisir |
| Bir DB satiri `db_host` / `yf_proxy_secret_key`i ezer | `ENV_ONLY_FIELDS` filtresi + S8.1 guvenlik citi |
| Yeni bir sir alani otomatik DB-yonetimli olur (fail-open) | S8.1 sir-adi citi |
| Shard'lar arasi yapilandirma carpikligi | S3.5 parent cozer, child tasir |
| `load_overrides` `get_settings()` cagirip RecursionError uretir | S8.1 casus testi + zorunlu `settings` parametresi |
| `.env` temizlendikten sonra downgrade config kaybettirir | `yfin config export --all`, depo DISINA; S9'da belgeli |
| Yeni bir `Settings` alani siniflandirilmadan eklenir | S8.1 tuketicilik citi |
| Paralel isler alan sayisini kaydirir | Sayilar (47 / 8 / 39 / 11 grup) **2026-09-05 anlik goruntusudur**; mekanizma tamlayan turetmedir, sayilara dayanmaz. v1 yaziminda bu sayi **uc kez** kaydi |
| PG gocu motor kararlarini degistirir | S2 motor notrlugu; kullanilan yardimcilar PG donusum tablosunda ayni adla yasiyor |

---

## 12. Bu tasarimin COZMEDIGI seyler

- **Calisan bir kosunun ortasinda ayar degistirmek.** Deger surec basinda bir
  kez okunur; degisiklik BIR SONRAKI kosuda etkili olur.
- **Degisiklik gecmisi / kim degistirdi.** `updated_at` disinda iz yok.
- **Esszamanli yazimda catisma tespiti.** Son yazan kazanir.
- **Cok kurulumlu merkezi yonetim.** Kurulum basina farklilastirma icin bir
  mekanizma YOKTUR.
- **Sirlarin yonetimi.** `yf_proxy_secret_key` ve `db_password` `.env`de kalir.

---

## Ek A — v1'den v2'ye

Uc bagimsiz inceleme (kod dogrulamasi, ic tutarlilik, tasarim boslugu)
asagidakileri buldu. Hepsi uygulandi.

**Hatalar:**

- `yf_discovery_enabled` diye bir alan **yok** (`config.py:79` kaldirildigini
  yaziyor); `discovery` grubu 9 degil **8**
- Alan sayilari 48 / 40 degil **47 / 39** (dokumanin on yerinde yanlisti)
- `get_settings()` cagri noktasi 42 degil **48**; dogrudan `Settings(...)`
  kurulumu 10 degil **13**
- `prune.py` yerel-import atfi `_asof_protected`ta **degil** (`:94`, `:134`, …)
- Atif hatalari: S5 -> S6.3, S3 -> S2, S6 -> S6.4

**Blokerler:**

- `sustainability.py:53` modul govdesinde `get_settings()` cagiriyor ->
  `yfin --help` DB'ye baglanirdi ve `log_level` gerekcesi cokerdi (S3.4)
- `yfin db create` var olmayan veritabanina baglanmaya calisirdi (S7, S10/A4)
- Autouse session fixture'i cok gec calisir; pytest once import eder (S8.3)

**Tasarim duzeltmeleri:**

- Hata koduna (`1146`) bakmak yerine `inspect(engine).has_table()` — motordan
  bagimsiz ve PG gocuyle uyumlu (S2)
- `load_overrides` uyarilari `configure_logging`den once basiliyordu (S3.3)
- Shard'lar kendi SELECT'lerini atiyordu; parent'in cozdugu deger tasinir (S3.5)
- `create_db_engine` yerine `NullPool` + `connect_timeout` (S2)
- `get_settings()` kilitsizdi (S2)
- `settings_schema()` sema ile durumu karistiriyordu; ikiye ayrildi (S6.4)
- `unset` ile `seed` birbirini bozuyordu; `seed` JSON kapsamina baglandi (S5.3)
- `export` model varsayilanlarini depoya civileyecekti; varsayilan yalnizca
  ezmeler, yedek depo disina (S6.2, S9)
- Tohumlamanin gerekcesi "panelin eksiksizligi" idi; panel zaten
  `settings_schema()`ten besleniyor. Gercek gerekce yazildi (S5.4)
- Serilestirme kurali ve gidis-donus garantisi yoktu (S4.4, S8.2)
- Panelin yazma yolu tanimsizdi; `set_setting()` saf fonksiyonu (S6.3)
- Anahtar harf duzeni kararlastirilmamisti (S2)
- `list --changed` tanimsizdi (S6.2)
- `YF_SETTINGS_SOURCE` sessizce basarisiz olabiliyordu (S3.1)
- `seed --force` semantigi belirsizdi (S5.2)
- Izin adimi eksikti (S9)
- Kapsam tek plana sigmiyordu; Plan A / Plan B'ye bolundu (S10)
