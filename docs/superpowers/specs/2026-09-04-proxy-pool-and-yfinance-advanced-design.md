# Proxy Havuzu ve yfinance Advanced Entegrasyonu — Tasarım Dokümanı

> **⚠️ MOTOR KARARLARI GEÇERSİZDİR — bu doküman tarihsel kayıttır.**
>
> Bu doküman MySQL 8 döneminde yazıldı. İçindeki **motora ait** her karar
> — kolon tipleri (`DATETIME(6)`, `LONGTEXT`, `MEDIUMTEXT`, `BIGINT UNSIGNED`),
> collation (`utf8mb4_*`, `ascii_bin`), `ON DUPLICATE KEY UPDATE`, `GET_LOCK`,
> partition tasarımı, `ERROR nnnn` kodları —
> `2026-09-04-postgres-timescaledb-migration-design.md` tarafından **geçersiz
> kılınmıştır**.
>
> İçindeki **veriye ait** kararlar (ölçülen alan uzunlukları, anahtar
> semantiği, as-of kapıları, monotonik kolonlar, kapsam kuralları, canlı
> API gözlemleri) **hâlâ geçerlidir ve tek kanıt kaynağıdır** — doküman bu
> yüzden silinmedi.


- **Tarih:** 2026-09-04
- **Durum:** Üç bağımsız incelemeden geçti, revize edildi
- **Proje kökü:** `~/Projects/learn/yfinance/`
- **Temel doküman:** `2026-09-04-yfinance-mysql-etl-design.md` (bundan sonra **T**,
  örn. T§7.1)
- **Kapsam:** Proxy havuzu (aktiflik + sağlık durumu), çok process'li shard'lı
  çalıştırma, ve `https://ranaroussi.github.io/yfinance/advanced/*` altındaki
  özelliklerin mevcut mimariye entegrasyonu

---

## 1. Amaç ve kapsam

### Kapsam içi

1. **`proxies` tablosu** — operatör kararı (`is_enabled`) ile sistem gözlemi
   (`health`) ayrı tutulan proxy havuzu; cooldown, ölü işaretleme, sayaçlar.
2. **Shard'lı çalıştırma** — proxy başına bir OS process; sembollerin dinamik
   iş kuyruğuyla dağıtımı; `runner.py`'nin üç fonksiyona ayrılması.
3. **yfinance advanced entegrasyonu** — `config.network.proxy`,
   `config.network.retries`, `config.debug.hide_exceptions`,
   `config.debug.logging`, `set_tz_cache_location()`, `history(repair=True)`.
4. **Hata sınıflandırması** — proxy sağlığını yalnızca ağ/ban sınıfı hataların
   etkilemesi.
5. **`dividends`/`splits`/`capital_gains`'in onarılmış history çerçevesinden
   beslenmesi** (§6.4) — T§6.3'ün "bağımsız çağrı" kararının geri alınması.
6. **CLI** — `yfin proxy` alt komut ağacı; `yfin sync --shards/--no-proxy/--require-proxy`.

### Kapsam dışı (bilinçli, YAGNI)

- **`config.locale`** — `yf.config` process-global olduğu için shard başına tek
  locale seçilebilir. Sembol başına locale ancak sembolleri borsaya göre
  shard'lamakla olurdu; bu, proxy-başına-shard eşlemesiyle çatışır.
- **`proxy_checks` geçmiş tablosu** — hangi proxy'nin hangi hücrede battığı
  `sync_run_items.proxy_id` ile sorgulanabilir (§3.5).
- **Proxy sağlayıcı API'lerinden otomatik proxy çekme** — proxy'ler CLI ile
  elle eklenir (T'nin "sembol evreni elle eklenir" ilkesiyle tutarlı).
- **Proxy ağırlıklandırma / kota** — proxy başına tam bir shard düşer.
- **`socks4`** — `curl_cffi` destekliyor, ama modern havuzlarda kullanılmıyor.
- **`multi_level_columns`** — `yf.download`/`Tickers` kullanılmadığı için
  (T§7.4) çok seviyeli kolon indeksi bu hatta hiç oluşmaz.
- **Başarısız sembolün aynı run içinde başka bir shard'da yeniden denenmesi**
  — kuyruk yalnızca **çekilmemiş** sembolleri kurtarır (§4.6).

---

## 2. API keşif bulguları — yfinance 1.7.0 kaynak kodu

Hepsi kurulu sürümün kaynağından doğrulandı
(`.venv/lib/python3.13/site-packages/yfinance/`).

### 2.1 `yf.config` process-global bir singleton'dır

`config.py:21-61` — `ConfigMgr` tek bir `self.options` dict'i tutar,
`threading.local` yoktur. `YfConfig = ConfigMgr()` modül seviyesinde tek örnek.

**Sessiz tuzak:** `NestedConfig.__getattr__` `self.data.get(key)` yapar
(`config.py:9-10`). Yanlış yazılmış bir anahtar **hata vermez, `None` döner**:
`yf.config.debug.hide_exception = False` (tekil) sessizce hiçbir şey yapmaz.
Bu yüzden `configure_yfinance()` sonunda atamaların uygulandığı doğrulanır
(§6.1).

### 2.2 `YfData` singleton'dır

`data.py:83` sınıf yorumu: *"Singleton means one session one cookie shared by
all threads."* `SingletonMeta.__call__` (`data.py:67-77`) `Ticker(symbol,
session=...)` çağrısında **yeni örnek yaratmaz**, mevcut singleton'ın oturumunu
`_set_session` ile değiştirir (`base.py:100`). `session is None` ise erken döner
(`data.py:136-137`), yani `Ticker("AAPL")` mevcut oturumu bozmaz.

### 2.3 Proxy config'ten okunup oturuma **koşullu** yazılır

`data.py:430-433` — `_make_request`, config'te proxy **`None` değilse** her
çağrıda `self._session.proxies = _normalize_proxy(YfConfig.network.proxy)`
yapar. `proxy = None` atamak daha önce yazılmış proxy'yi **temizlemez**
(upstream bunu bilinçli yapıyor: kullanıcının doğrudan session'a koyduğu proxy
silinmesin). `_set_session` de aynı koşullu yazımı yapar (`data.py:150-151`).

**Sonuç:** proxy'siz çalıştırma her zaman **taze bir process**te olmalıdır.

### 2.4 Tek process içinde istek başına proxy rotasyonu imkânsızdır

Dört worker thread'i aynı anda farklı proxy kullanamaz; biri diğerinin
proxy'sini ezer. **Rotasyon ekseni process'tir, thread değil** (§4).

| Alternatif | Neden elendi |
|---|---|
| Run başına tek proxy (process-global) | Rotasyon yok; tek proxy banlanınca run biter |
| Kendi oturum enjeksiyonumuz (thread-local) | `YfData`'nın cookie/crumb yönetiminin içine girmeyi gerektirir; upstream'in özel API'sine bağımlı, her sürüm yükseltmesinde kırılır |
| **Proxy-shard'lı multiprocess** | **Seçildi.** `yf.config`'in global olması shard sınırında sorun değil |

### 2.5 `retries = 0` iç retry'ı kapatır — ama tek retry otoritesi olmaz

`retries` varsayılanı `0`'dır (`config.py:31`) ve **tek yerde** kullanılır
(`data.py:473-481`). Elemesi `_is_transient_error` (`data.py:19-28`), ki
`isinstance(exc, OSError)` kontrolü yapar — ve
`curl_cffi.requests.exceptions.RequestException` **`OSError` türevidir**. Yani
`retries > 0` olsaydı `HTTPError` dahil bütün curl_cffi istisnaları yeniden
denenirdi. `0`'a sabitlemek bunu tamamen kapatır.

**Ama kapatılamayan ikinci bir retry vardır.** `data.py:483-506`: HTTP ≥400
gelen **her** yanıtta yfinance cookie stratejisini toggle eder (jar temizlenir),
cookie + crumb'ı yeniden çeker ve isteği **bir kez daha** atar. Bu davranış
`retries`'ten bağımsızdır.

**Sonuç — tasarımı bağlar:** tenacity'nin bir denemesi ≥2 gerçek Yahoo isteğine
karşılık gelir. Token-bucket bütçesi ve `YF_PROXY_FAILURE_THRESHOLD` bu ~2×
çarpanı hesaba katar.

### 2.6 `hide_exceptions` ve `set_cache_location`

- `hide_exceptions` varsayılanı `True`; bayrak **52 yerde** okunur (§6.2).
- `set_tz_cache_location()` aslında `set_cache_location()`'a delege eder ve
  **üç** cache'i birden taşır: tz (`tkr-tz.db`), cookie (`cookies.db`) ve ISIN
  (`isin-tkr.db`) — `cache.py:625-638`. Üçü de peewee `SqliteDatabase`,
  `journal_mode=wal`. **İsim yanıltıcıdır.**
- `_http.py:new_session()` `curl_cffi` varsa `impersonate="chrome"` ile oturum
  kurar; `YF_DISABLE_CURL_CFFI` ile düz `requests`'e düşer. `curl_cffi`
  `http`/`https`/`socks5`/`socks5h` şemalarını destekler (`utils.py:838-879`;
  yerel denemeyle doğrulandı). `https://` şeması `CurlCffiWarning` üretir —
  çoğu kullanıcı aslında CONNECT-tünel için `http://` ister.

---

## 3. Veri modeli

### 3.1 `proxies` tablosu

Mevcut konvansiyonlar korunur: `MYSQL_TABLE_ARGS`, `TsType()` (DATETIME(6)),
ağ alanlarında ascii collation (T§5.1).

| Kolon | Tip | Null | Varsayılan | Not |
|---|---|---|---|---|
| `id` | `BIGINT UNSIGNED` | H | AUTO_INCREMENT | PK |
| `label` | `VARCHAR(64)` ascii_bin | H | — | UNIQUE; CLI bununla hedefler |
| `scheme` | `ENUM('http','https','socks5','socks5h')` | H | — | |
| `host` | `VARCHAR(255)` ascii_general_ci | H | — | hostname veya IP |
| `port` | `SMALLINT UNSIGNED` | H | — | |
| `username` | `VARCHAR(128)` | **H** | `''` | boş dize = kullanıcısız (§3.3) |
| `password_enc` | `VARBINARY(512)` | E | NULL | Fernet token'ı — base64 **ASCII** (§3.4) |
| `is_enabled` | `BOOLEAN` | H | `1` | **operatör kararı** |
| `health` | `ENUM('unknown','healthy','cooldown','dead')` | H | `'unknown'` | **sistem kararı** |
| `cooldown_until` | `DATETIME(6)` | E | NULL | |
| `consecutive_failures` | `INT` | H | `0` | ardışık; eşiği aşınca cooldown |
| `cooldown_rounds` | `INT` | H | `0` | kümülatif; eşiği aşınca dead (§5.4) |
| `success_count` / `failure_count` | `BIGINT` | H | `0` | kümülatif |
| `last_ok_at` / `last_error_at` / `last_checked_at` | `DATETIME(6)` | E | NULL | |
| `last_error` | `TEXT` | E | NULL | `ErrorKind` + redakte edilmiş mesaj |
| `last_latency_ms` | `INT` | E | NULL | aktif kontrolden |
| `created_at` / `updated_at` | `DATETIME(6)` | H | `CURRENT_TIMESTAMP(6)` | |

**Kısıtlar ve indeksler**

- `UNIQUE (label)`
- `UNIQUE (scheme, host, port, username)` — `username` **NOT NULL DEFAULT `''`**
  olduğu için kısıt gerçekten uygulanır. (NULL'lu bir kolonda MySQL tekilliği
  zorlamaz; bu yüzden nullable tasarımdan vazgeçildi. `add` komutundaki varlık
  kontrolü yalnızca kullanıcı dostu hata mesajı içindir, tekilliğin garantisi
  değildir — TOCTOU yarışını DB kısıtı kapatır.)
- `INDEX ix_proxies_eligibility (is_enabled, health)` — §5.1 sorgusunda
  `health <> 'dead'` negasyon, `cooldown_until` aralık koşuludur; index yalnızca
  `is_enabled` öneki için etkin kullanılır. Onlarca satırlık bir tabloda bu
  yeterlidir; index "sorguyu hızlandırır" diye değil, tam tarama maliyetini
  sabitlemek için vardır.

**Uygulama notları**

- ENUM'lar mevcut konvansiyonla tanımlanır:
  `Enum(ProxyHealth, values_callable=lambda e: [m.value for m in e])`
  (`models/sync.py:37-39`). Bu olmadan SQLAlchemy enum **isimlerini**
  (`HEALTHY`) yazar.
- `models/base.py`'ye iki tip fabrikası eklenir: `ProxyLabelType()`
  (`VARCHAR(64) ascii_bin`) ve `HostType()` (`VARCHAR(255) ascii_general_ci`).
- `created_at`/`updated_at` için `server_default=text("CURRENT_TIMESTAMP(6)")` +
  `server_onupdate=FetchedValue()` kullanılır. Bu, kod tabanında **yeni bir
  konvansiyondur** (mevcut `server_default`'lar sabit değerlerdir); `migrations/
  env.py`'deki `compare_type=True` ile autogenerate'in bunu her koşuda
  "değişti" saymadığı §10.2'deki drift testiyle doğrulanır.
- **ENUM değer değişikliklerini Alembic autogenerate yakalamaz**; ilgili
  migration adımları elle yazılır (§9).

### 3.2 Neden iki ayrı aktiflik kolonu

`is_enabled` **operatörün niyetidir**; sistem asla değiştirmez.
`health` **sistemin gözlemidir**; CLI onu yalnızca `yfin proxy reset` ile
sıfırlar (§5.5).

Tek bir `is_active` kolonu bunları karıştırırdı: otomatik ban tespiti,
operatörün bilerek kapattığı bir proxy'yi başarılı bir istek sonrası yeniden
açabilir; ya da tersine, operatörün elle açması sistemin ban bilgisini silerdi.
Bu ayrım, T§5.2'deki `symbols.is_active` (kullanıcı kararı) ile
`symbols.unknown_streak` (sistem sayacı, T§8.8) ayrımının aynısıdır.

**Uygunluk (eligibility) — tek doğruluk kaynağı:**

```
is_enabled = 1
AND health <> 'dead'
AND (cooldown_until IS NULL OR cooldown_until <= NOW(6))
```

`health = 'unknown'` **uygundur** (yeni eklenmiş, hiç denenmemiş proxy).

### 3.3 `username` neden NOT NULL

MySQL'de UNIQUE index NULL içeren satırlar için tekilliği zorlamaz; aynı
`(scheme, host, port, NULL)` demeti defalarca eklenebilirdi. `''` (boş dize)
"kullanıcısız" anlamına gelir, kısıt gerçekten çalışır, ve `add`'in varlık
kontrolü NULL-safe karşılaştırma (`<=>`) gerektirmez.

### 3.4 Parola saklama

Proxy parolası düz metin yazılmaz. Zaten doğrudan bağımlılık olan
`cryptography`'nin `Fernet`'i ile `.env`'deki `YF_PROXY_SECRET_KEY` altında
şifrelenir. Fernet token'ı URL-safe base64 **ASCII**'dir; `VARBINARY(512)`
seçilir çünkü charset/collation dönüşüm riskini sıfırlar. Ölçüldü: 512 bayta
sığan azami düz metin ≈ 310 bayt — tipik parola için fazlasıyla yeterli.

- Anahtar yoksa **parolalı** proxy eklenemez; `yfin proxy add` açık hata verir.
  Parolasız proxy anahtarsız da eklenebilir.
- Anahtar değişirse mevcut `password_enc` çözülemez. Bu durum proxy'yi `dead`
  **yapmaz**; `proxy check` açık bir "parola çözülemedi" hatası raporlar ve
  proxy sağlığına dokunmaz (yanlış teşhis üretmemek için).

**Redaction — mutlak kural.** Parola hiçbir log satırına, hata mesajına,
`proxy list` çıktısına veya `sync_run_items.error`/`proxies.last_error` alanına
girmez. Bizim ürettiğimiz her satırda `label` kullanılır. **Ayrıca:** DSN
`yf.config.network.proxy`'ye düz metin yazıldığı için upstream'in debug logları
veya bir traceback parolayı basabilir — bu bizim kontrolümüzde değildir. Bu
yüzden §6.5'teki structlog köprüsüne **zorunlu bir redaction processor** eklenir
(`scheme://user:***@host:port` deseni).

Elenen alternatif: kolonun bir env değişkeninin *adını* tutması. Anahtar
yönetimi gerektirmez ama 20 proxy = 20 env değişkeni demektir.

### 3.5 `sync_runs` / `sync_run_items` genişletmesi

| Tablo | Yeni kolon | Tip | Not |
|---|---|---|---|
| `sync_runs` | `shard_count` | `SMALLINT` NOT NULL DEFAULT 1 | proxy seçiminden **sonra** yazılır (§4.4) |
| `sync_run_items` | `shard_index` | `SMALLINT` NOT NULL DEFAULT 0 | |
| `sync_run_items` | `proxy_id` | `BIGINT UNSIGNED` NULL | **FK YOKTUR** (aşağıda) |
| `sync_run_items` | `proxy_label` | `VARCHAR(64)` ascii_bin NULL | anlık kopya; proxy silinse de denetim okunur |

**`proxy_id`'de FK neden yok.** İki gerekçe:

1. **Kilit çakışması.** InnoDB her `INSERT INTO sync_run_items` için ebeveyn
   `proxies` satırına **shared lock** alır. Child'lar toplu item yazarken kendi
   proxy satırını S-kilitler; §5.4'teki sağlık flush'ı (`UPDATE proxies SET
   health=...`) aynı satırda X-lock ister → lock wait / deadlock. FK, tam da
   engellemek istediğimiz sorunu üretirdi.
2. **Konvansiyon.** `sync_run_items.symbol` de aynı gerekçeyle FK'sız
   (`models/sync.py:62-64`): denetim kaydı, referans ettiği varlık silinse de
   ayakta kalmalıdır.

`INDEX ix_sync_run_items_proxy (proxy_id, status)` — "bu proxy hangi hücrelerde
battı" sorgusu için.

### 3.6 `ItemStatus`'a yeni değer: `not_attempted`

Bir run'da kuyrukta işlenmeden kalan sembol için yazılır (§4.7). Mevcut
`skipped` **kullanılamaz**: onun anlamı "içerik hash'i değişmedi"dir ve
`not_attempted` ile karıştırılırsa "veri güncel" yanılsaması üretir.

### 3.7 `price_history` genişletmesi

| Kolon | Tip | Not |
|---|---|---|
| `is_repaired` | `BOOLEAN` NOT NULL DEFAULT `0` | yfinance `"Repaired?"` kolonundan (§6.3) |

---

## 4. Mimari — koordinatör ve shard'lar

### 4.1 Yeni ve değişen modüller

```
src/yfin/
  errors.py             # ErrorKind + classify_error (yfinance'e BAGIMLI DEGIL)
  shard.py              # koordinatör: process yönetimi, kuyruk, iptal protokolü
  proxy/
    dsn.py              # adres değer nesnesi (saf)
    crypto.py           # Fernet şifreleme
    health.py           # durum makinesi (SAF, I/O yok)
    repository.py       # proxies tablosu + ShardProxyTracker
    check.py            # ham curl_cffi kontrolü
  models/proxies.py
  scripts/seed_proxies.py   # toplu doldurma (stdin veya dosya)
```

**`errors.py` neden ayrı:** `ErrorKind` başlangıçta `client.py`'deydi ve
`proxy` paketi onu almak için yfinance sarmalayıcısını import etmek zorunda
kalıyordu — proxy alan modelinden yfinance'e doğru **ters bir bağımlılık oku**.
Ayrı modül bu oku kaldırır ve `client.py`'yi 355 → 167 satıra indirir.

**`proxy` neden paket:** tek dosyada DSN değer nesnesi, şifreleme, **saf durum
makinesi**, DB deposu ve HTTP kontrolü bir aradaydı. Saf, I/O'suz çekirdeğin
(`health.py`) DB ve ağ kodundan ayrılması SRP'nin somut karşılığıdır; en
kritik politika artık hiçbir I/O'ya bulaşmadan test edilebiliyor.

**`runner.py` üçe ayrılır.** Bugün `run_sync` (runner.py:273-405) dört
sorumluluğu birden taşıyor: advisory lock (satır 291), `SyncRun` satırını açmak
(307-316), producer/consumer döngüsü, ve `_finalize`'da hem item hem run
yazımı (408-444). Parent/child ayrımı bunu bölmeyi zorunlu kılar:

```python
def open_run(factory, *, symbol_count, dataset_count, shard_count) -> int
def run_shard(engine, symbols: SymbolSource, datasets, *, run_id, shard_index,
              proxy_id, proxy_label, full_refresh, settings) -> ShardCounters
def finalize_run(factory, run_id) -> RunSummary
```

`run_shard` `sync_run_items` satırlarını **kendi içinde** yazar. `run_sync`
geriye uyumlu bir sarmalayıcı olarak kalır (tek shard, kuyruk yerine liste) —
mevcut testler ve kütüphane kullanımı kırılmaz.

### 4.2 Registry ile uyum — doğrulandı

Registry `Registry[D: Registrable]` jenerik sınıfıdır ve **iki örneği** vardır:
`SYMBOL_DATASETS` (bootstrap `"symbols"`, alias'lar `actions`/`financials`) ve
`MARKET_DATASETS` (bootstrap yok). `yfin/datasets/__init__.py` alt modülleri
import eder, her modül sonunda `register(...)` çağrılır. `Dataset[P]` jenerik
bir tabandır ve örnekleri **durumsuzdur** — sembol durumunun tamamı
`SyncContext`'tedir.

**Piyasa dataset'leri proxy kullanır ama shard'lanmaz.** `MARKET_DATASETS`
(`GlobalDataset`) sembol eksenli değildir — her dataset zaten tek bir global
çağrıdır, dolayısıyla kuyruk ve shard'lama anlamsızdır. `run_market_sync`
havuzdan **tek** proxy seçer, `configure_yfinance` ile ona bağlanır ve sağlık
muhasebesini aynı `ShardProxyTracker` ile tutar; uygun proxy yoksa doğrudan
bağlanır (sembol tarafıyla aynı politika).

**Shard modeli yalnızca `SYMBOL_DATASETS`'i kapsar.** `MARKET_DATASETS`
(`GlobalDataset`) sembol eksenli değildir. **Düzeltme:** ilk taslak
`yfin/datasets/market/`'i "henüz mevcut değil" sayıyordu; F §6.5/§7.2 ile
altı piyasa dataset'i ve `market_runner.py` UYGULANMIŞTIR. Geçerli kural:
piyasa tarafı ayrı bir `yfin market sync` komutudur (F §7.2, ayrı
`yfin_market_sync` advisory kilidi), shard'lanmaz ve havuzdan tek proxy
alır. Sembol kuyruğuna sokmak N kez tekrar çekilmelerine yol açardı.

**Determinizmin gerçek kaynağı.** Registry'nin iç sözlüğünün sırası
`__init__.py` import listesinden **gelmez**, transitif import grafiğinden gelir
(`fast_info.py` ve `history_metadata.py` `symbols` modülünü listeden önce
tetikler). **Ama bu bizi ilgilendirmez:** child'a *adlar* geçirildiği için
`SYMBOL_DATASETS.resolve(names)` çıktısı kayıt sırasından bağımsızdır —
determinizm `Registry._expand`'in sıra koruyan tekilleştirmesinden gelir.

Buradan iki kural:

1. **Child'a `Dataset` nesnesi değil, çözülmüş dataset *adları* (`list[str]`)
   geçilir**; child kendi tarafında `SYMBOL_DATASETS.resolve(names)` çağırır.
2. **`spawn` start method açıkça seçilir.** macOS/Python 3.13'te bu zaten
   varsayılandır; açık seçim Linux (3.13 varsayılanı `fork`) için taşınabilirlik
   gereğidir. `fork`'ta paylaşılan soketler ve `curl_cffi`'nin CFFI handle'ları
   bozuk davranır. `spawn`'ın iki ek şartı:
   - `Process(target=...)` hedefi **modül seviyesinde import edilebilir bir
     fonksiyon** olmalı (closure/lambda/instance-metodu olamaz); `ShardSpec`
     `__main__` dışında bir modülde tanımlanmalı.
   - `mp.Queue`'dan geçen her şey pickle'lanabilir olmalı — bu şart start
     method'tan **bağımsızdır**. **Ham istisna nesneleri kuyruğa konmaz:**
     child istisnayı kendi tarafında `classify_error` ile `ErrorKind`'a çevirip
     `(ErrorKind, str)` olarak geçirir. (`YFTickerMissingError` ailesinin
     `__init__` imzası pickle round-trip'inde `TypeError` üretir —
     `exceptions.py:22`.)

`spawn`'ın yan faydası: `client.py`'deki `_bucket` ve `config.py`'deki
`_settings` process-global olduğu için her shard kendi token-bucket'ını kurar ve
rate limit proxy (çıkış IP'si) başına anlam kazanır (§7.2).

### 4.3 Child'ın başlangıç sırası — bağlayıcı

`spawn` sonrası child şu sırayı **tam olarak** izler:

1. `configure_logging(settings.log_level)` — **ilk adım.** `structlog`
   `cache_logger_on_first_use=True` (`logging_setup.py:23`) ile çalışır; modül
   seviyesindeki logger'lar (`client.py:22`, `runner.py:26`) ilk kullanımda
   yapılandırmayı önbelleğe alır. Bundan önce log basılırsa child sessizce
   yapılandırılmamış `PrintLogger`'a düşer ve §6.5'in tüm alan bağlaması
   kaybolur.
2. `yfin.datasets` import edilir (registry kurulur), `SYMBOL_DATASETS.resolve(names)`.
3. structlog köprüsü `logging.getLogger("yfinance")`'a bağlanır (§6.5) —
   **`yf.config.debug.logging = True` atamasından önce.**
4. `configure_yfinance(dsn, proxy_key)` (§6.1).
5. Kendi engine'i: `create_db_engine(settings, spec.database, pool_size=3)`.
6. Kuyruktan sembol çekme döngüsü.

### 4.4 Parent (koordinatör) akışı

1. **Advisory lock alınır** — tek nokta (T§8.7). Child'lar kilit almaz.
   `run_sync`'in `acquire_lock=False` parametresi (runner.py:280) bunu zaten
   destekliyor.
2. `proxy.select_eligible(limit=effective_shards)` uygun proxy'leri çeker (§5.1).
3. **Shard sayısı tek formülle belirlenir:**
   ```
   shard_count = 1                                  if --no-proxy
               = max(1, min(N_effective, |eligible|))  aksi halde
   ```
   `N_effective` = `--shards` verilmişse o, yoksa `YF_MAX_SHARDS`.
   **Proxy'siz shard asla açılmaz** — tek istisna, havuzun tamamen boş/uygunsuz
   olduğu tek-shard doğrudan bağlantı hali (§4.8).
4. `open_run(...)` — `sync_run` satırı açılır ve **commit edilir**; `run_id`
   ancak ondan sonra child'lara geçer. (Child ayrı bir bağlantı kullanır;
   commit edilmemiş bir `run_id`'ye item yazmak `ERROR 1452` verirdi.)
   `shard_count` bu adımda bilinir çünkü proxy seçimi 2. adımda yapıldı.
5. `multiprocessing.get_context("spawn")` ile `shard_count` child başlatılır.
   Her child'a **`ShardSpec`** geçer: `run_id`, `shard_index`, `proxy_id`,
   `proxy_label`, çözülmüş DSN, **`database`**, dataset adları, `full_refresh`,
   cache dizini. *(`database` alanı şart: testler `yfinance_test` şemasına
   bağlanıyor — `tests/conftest.py:38`; olmadan child üretim şemasına yazardı.)*
6. Semboller `mp.Queue`'ya doldurulur, sonuna `shard_count` adet sentinel konur.
   Child'lar boşaldıkça birer sembol çeker.
7. **Tüm child'lar `join(YF_SHARD_TIMEOUT_SECONDS)` edilir.** Süreyi aşan child
   SIGTERM → kısa bekleme → SIGKILL ile sonlandırılır ve §5.4'teki
   `SHARD_CRASH` olayı uygulanır.
8. `finalize_run(...)` — toplamlar ve çıkış kodu **DB'den** hesaplanır (§4.5).
9. Advisory lock **ancak burada** bırakılır.

### 4.5 Toplamların tek doğruluk kaynağı DB'dir

Parent'a "sonuç kuyruğu" ile özet sayaç **taşınmaz**. Sebep: `sync_runs`
toplamları kuyruktan, `sync_run_items` gerçeği child'lardan gelseydi ikisi
birbirini tutmayabilirdi ve T§8.6'nın "makine tarafından doğrulanabilir
eksiksizlik" iddiası zayıflardı. Bunun yerine `finalize_run` üç sorgu koşar:

```sql
-- durum dağılımı ve satır toplamları
SELECT status, COUNT(*), SUM(rows_fetched), SUM(rows_written),
       SUM(rows_verified), SUM(rows_skipped)
FROM sync_run_items WHERE run_id = :r GROUP BY status;

-- çözülmüş sembol sayısı (RunSummary.resolved_symbols karşılığı)
SELECT COUNT(*) FROM (
  SELECT symbol FROM sync_run_items WHERE run_id = :r
  GROUP BY symbol HAVING SUM(status = 'unknown_symbol') = 0
) t;
```

Bu, `RunSummary.exit_code()`'un (runner.py:77-86) ihtiyaç duyduğu üç girdiyi de
verir. Child'ların parent'a döndürdüğü `ShardCounters` yalnızca **canlı ilerleme
logu** içindir ve **yetkili değildir**; spec bunu böyle işaretler.

**Bariyer:** agregasyon, tüm child'lar `join()` edildikten (ve son
transaction'ları commit edildikten) sonra koşar. Sıra: join → SHARD_CRASH işle →
`not_attempted` yaz (§4.7) → agrega → `sync_runs` finalize → lock bırak.

### 4.6 Sembol düzeyinde yeniden deneme yoktur — açık karar

Kötü bir proxy'de düşen sembol o run için kalıcı olarak `failed` kalır; dinamik
kuyruk yalnızca **henüz çekilmemiş** sembolleri kurtarır. Yeniden deneme run
düzeyindedir (cron bir sonraki turda dener). Aynı sembolü başka bir shard'a
yeniden kuyruklamak, sembol başına tek transaction ilkesiyle (T§8.7) ve
`sync_run_items` denetim kaydının tekilliğiyle çatışırdı.

### 4.7 Bir shard'ın kendini geri çekmesi ve işlenmemiş semboller

**Child kendi proxy'sini cooldown'a soktuğu anda kuyruktan yeni sembol çekmeyi
durdurur**, elindeki sembolü bitirir, sağlığı flush eder ve temiz çıkar.
Bu kural olmadan "cooldown'a giren proxy kendiliğinden daha az sembol alır"
varsayımı **tersine dönerdi**: banlanmış bir proxy 429/403'ü anında aldığı için
kuyruktan en çok sembolü o çeker ve hepsini `failed` işaretler.

Sonuç olarak run sonunda kuyrukta işlenmemiş sembol kalabilir. Parent bunların
**her biri için `status='not_attempted'` bir `sync_run_items` satırı yazar**
(`dataset='symbols'`, `proxy_id=NULL`). Aksi halde bu semboller için hiç satır
olmaz, `failed` sayısı sıfır kalır ve **evrenin yarısı hiç çekilmemişken run
`ok` + exit 0 dönerdi**; `sync_runs.symbol_count` ile item sayısı da tutmazdı.

### 4.8 `--no-proxy`, boş havuz ve `--require-proxy`

Üç hal ayrı davranır:

| Hal | Shard | Proxy sağlığı | `proxy_id` | Ek |
|---|---|---|---|---|
| `--no-proxy` | 1 (zorunlu) | **hiç güncellenmez** | NULL | Kullanıcı bilinçli baypas ediyor; hiçbir proxy cezalandırılmaz |
| Havuz boş (hiç kayıt yok) | 1 | — | NULL | INFO log |
| Havuz dolu ama hiçbiri uygun | 1 | — | NULL | **WARNING**: "N proxy var, hiçbiri uygun değil; doğrudan bağlanılıyor" |
| `--require-proxy` + uygun proxy yok | — | — | — | `EXIT_NO_PROXY` ile durur (§8.1) |

`--no-proxy` ile `--shards N` birlikte verilirse **N zorla 1'e indirilir** ve
uyarı basılır: aynı çıkış IP'sinden N shard koşmak toplam hızı N katına çıkarır
(§7.2).

### 4.9 İptal protokolü (SIGINT / SIGTERM)

Tanımsız bırakılırsa üç somut arıza doğar: (a) parent ölünce MySQL `GET_LOCK`
oturum kapanışıyla **serbest kalır** ama spawn edilmiş child'lar yaşamaya ve
yazmaya devam eder → yeni bir cron tetiklemesi kilidi alır ve T§8.7'nin garantisi
düşer; (b) dolu bir `mp.Queue` üzerinde `join()` klasik feeder-thread deadlock'u
verir; (c) yarım kalan `sync_runs` satırı `running` durumunda asılı kalır.

Protokol:

1. Parent SIGINT/SIGTERM yakalar, kuyruğu drain edip sentinel basar.
2. Child'lara SIGTERM → `join(grace)` → kalanlara SIGKILL.
3. **Advisory lock, child'ların tamamı sonlanmadan bırakılmaz.**
4. Kalan semboller `not_attempted` yazılır, `sync_runs` `failed` olarak finalize
   edilir.
5. Kuyruklar `join()`'dan **önce** drain edilir.

### 4.10 Paylaşılan tablolar: `news` + `news_symbols`

`news` sembolden bağımsızdır (aynı haber birden çok sembolde çıkar) ve
`news_symbols` satırları haberin ticker listesinden üretilir
(`datasets/news.py:170-186`) — bir AAPL haberi `^GSPC`, `005930.KS` satırları
yazar. Semboller shard'lara dağıtıldığı için:

1. İki process aynı `news.news_id` üzerinde eşzamanlı `ON DUPLICATE KEY UPDATE`
   yapabilir.
2. İki process aynı `(news_id, symbol)` satırını eşzamanlı upsert edebilir.
3. `news_symbols.news_id` → `news.news_id` **FK'sı vardır**
   (`models/news.py:61`): A process'i `news` satırını yazıp commit etmemişken B
   process'i aynı `news_id` için `news_symbols` insert ederse FK kontrolü A'nın
   satırında S-lock bekler. Klasik çapraz deadlock deseni; tek process'te hiç
   oluşmuyordu.

**Çözüm:** sembol transaction'ı deadlock (1213) veya lock-timeout (1205)
aldığında kısa jitter'lı yeniden deneme (`YF_TXN_RETRY_ATTEMPTS`, varsayılan 3).
Transaction sembol kapsamlı (T§8.7) ve idempotent (T§7.2) olduğu için yeniden
çalıştırmak güvenlidir.

**Uygulama zorunluluğu:** MySQL varsayılanında (`innodb_rollback_on_timeout=OFF`)
**ERROR 1205 yalnızca son ifadeyi geri alır**, transaction yaşamaya devam eder.
Yeniden denemeden önce `session.rollback()` **zorunludur** — mevcut `except`
bloğu (runner.py:389) bunu yapıyor, retry döngüsü eklenirken kaybedilmemeli.

**Ölçüm uyarısı:** `MySQLRowWriter._verify` (persistence.py:107-135) bir
**varlık sayımıdır**, satırın yazarını ayırt etmez. Sessiz yanlış doğrulama
üretmez (ODKU anahtarı her hâlükârda kalıcılaştırır), ama `news`/`news_symbols`
için `rows_written`/`rows_verified` shard'lar arasında **çift sayılabilir**. Bu
iki tablo için `sync_runs` toplamları bir **üst sınırdır**; tekil satır sayısı
`SELECT COUNT(*) FROM news` ile alınır. Bu, T§8.6 doğrulama garantisini
bozmaz (hücre bazlı `verified == attempted` kontrolü shard içinde doğru kalır).

### 4.11 tz/cookie/ISIN cache izolasyonu

yfinance'in üç cache'i de peewee/SQLite'tır (`cache.py:625-638`, WAL). WAL tek
yazıcıya izin verir; N process aynı dosyaya yazarsa ikinci yazıcı `SQLITE_BUSY`
→ `OperationalError: database is locked` alır.

Her child kendi dizinini alır ve **dizin `shard_index` ile değil, proxy kimliği
ile anahtarlanır**:

```python
yf.set_tz_cache_location(f"{YF_TZ_CACHE_DIR}/{proxy_key}")
# proxy_key = f"proxy-{proxy_id}"  ya da proxy yoksa "direct"
```

`shard_index` ile anahtarlamak yanlış olurdu: proxy seçimi latency/health'e göre
sıralandığı için `shard-0` bir sonraki run'da **başka bir proxy** olabilir ve A
proxy'sinin IP'siyle mintlenmiş cookie, B'nin çıkış IP'siyle kullanılırdı.

Bu izolasyonun gerekliliği ölçüldü: cookie cache'in birincil anahtarı
**`strategy`**'dir (`cache.py:314`) — yalnızca `'basic'`/`'csrf'` iki satır.
Ortak bir `cookies.db`'de bütün shard'lar birebir aynı iki satırı ezerdi;
IP-cookie uyuşmazlığı teorik değil, kesin olurdu.

### 4.12 Bağlantı bütçesi

`create_db_engine` bugün `pool_size = max(5, yf_max_workers + 4)` ve
`max_overflow = pool_size` veriyor (db.py:22-32) → **process başına 16 bağlantı
tavanı**. 4 shard + parent = 5 process → 80; `YF_MAX_SHARDS`'ı 8'e çıkarmak
MySQL varsayılanı `max_connections=151`'i zorlar ve `ERROR 1040` üretir — bu
hata `classify_error`'da `DATA`'ya düşer (proxy'yi haksız suçlamaz) ama tüm
shard'ı öldürür.

Gerçek eşzamanlı tüketici sayısı process başına yalnızca **ikidir**:
`WatermarkReader` kendi `threading.Lock`'u ile tüm watermark okumalarını
serileştiriyor (runner.py:108), artı ana thread'in sembol transaction'ı.

**Karar:** `create_db_engine`'e `pool_size` parametresi eklenir; child'lar
`pool_size=3, max_overflow=2` ile kurar.

**Invariant (§7'de de yazılı):** `(shard_count × 5) + 2 ≤ MySQL max_connections`.
`+2`: parent'ın kendi havuzu ve run boyunca **checked-out** tuttuğu advisory
lock bağlantısı (db.py:45).

---

## 5. Proxy politikası

### 5.1 Seçim

```sql
SELECT * FROM proxies
WHERE is_enabled = 1
  AND health <> 'dead'
  AND (cooldown_until IS NULL OR cooldown_until <= NOW(6))
ORDER BY (health = 'healthy') DESC,
         (health = 'unknown') DESC,      -- hiç denenmemiş proxy öne
         last_latency_ms IS NULL,        -- NULL'lar sona
         last_latency_ms ASC,
         id ASC
LIMIT :shard_count
```

`(health='unknown') DESC` kriteri gereklidir: o olmadan `last_latency_ms IS
NULL` yüzünden **yeni eklenmiş bir proxy hiç kullanılmaz**, buna karşılık
cooldown'dan yeni çıkmış (ve geçmişte latency ölçülmüş) bir proxy tercih
edilirdi. `id ASC` sonda: eşitlikte sıra deterministik kalır, testler
tekrarlanabilir olur.

Eşzamanlı iki sync olamayacağı için (advisory lock) proxy "rezervasyonu"
gerekmez.

### 5.2 Hata sınıflandırması

Bugün `client.py` yalnızca "retry edilebilir mi" diye sorar. Proxy sağlığı için
bu yetmez: **her hata proxy'nin suçu değildir.**

```python
class ErrorKind(StrEnum):
    RATE_LIMITED = "rate_limited"
    BLOCKED = "blocked"
    NETWORK = "network"
    DATA = "data"
    UNKNOWN_SYMBOL = "unknown_symbol"
```

**Sıra bağlayıcıdır** — `curl_cffi.requests.exceptions.RequestException`
**`OSError` türevidir**, bu yüzden bir `OSError → NETWORK` kuralı 403'leri de
`NETWORK` sanar ve proxy'yi haksız cezalandırırdı:

1. `YFRateLimitError` → `RATE_LIMITED` (`data.py:506`)
2. `HTTPError` + `response.status_code`: `429` → `RATE_LIMITED`;
   `401/403` → `BLOCKED`; `5xx` → `NETWORK`; diğer `4xx` → `DATA`
3. yfinance tipleri: `YFTzMissingError` → `UNKNOWN_SYMBOL`;
   `YFPricesMissingError`, `YFInvalidPeriodError`, `YFDataException` → `DATA`
4. `curl_cffi.requests.exceptions.{ProxyError, DNSError, ConnectionError,
   Timeout, ConnectTimeout, ReadTimeout, SSLError}` → `NETWORK`
5. `_NEVER_RETRYABLE` (programlama/veri hataları) → `DATA`
6. Yerleşik `TimeoutError` / `ConnectionError` → `NETWORK`
7. Metin geri düşüşü: mevcut `_HTTP_STATUS_RE` + `_HTTP_CONTEXT_RE` ve
   `_RETRYABLE_MARKERS` (client.py:88-107)

**`YFPricesMissingError` `UNKNOWN_SYMBOL` DEĞİL `DATA`'dır** — anlamı "bu
aralıkta fiyat yok" (tatil, yeni IPO, kapalı borsa), "sembol geçersiz" değil.

**Ölü kod temizliği.** `_NEVER_RETRYABLE`, `_RETRYABLE_STATUS` ve
`_STATUS_PATTERN` bugün tanımlı ama `is_retryable` hiçbirini kullanmıyor
(client.py:69-83 vs 112-123); satır 65-68'deki yorum bunun aksini iddia ediyor.
`classify_error` yazılırken `_NEVER_RETRYABLE` **gerçekten ilk elemeye** alınır,
`_RETRYABLE_STATUS` ve `_STATUS_PATTERN` **silinir** (`_HTTP_STATUS_RE` onların
yerini almış).

**`is_retryable = kind in {RATE_LIMITED, NETWORK}`.** `BLOCKED` **retry
edilmez** — bu bugüne göre bir davranış değişikliği değil (mevcut regex yalnız
`429|5\d\d` yakalıyor) ve bilinçlidir: banlanmış bir proxy'de 5 deneme ×
`wait_exponential_jitter(max=16)` ≈ 30+ sn ile token-bucket boşa harcanırken
§5.4 zaten o proxy'yi cooldown'a alacaktır.

### 5.3 Retry maliyeti ~2× hesaba katılır

§2.5 gereği tenacity'nin **bir** denemesi ≥2 gerçek Yahoo isteğidir (cookie
stratejisi toggle + crumb yenileme + isteğin tekrarı). Bu yüzden:

- Token-bucket bütçesi planlanırken efektif istek sayısı `2 × çağrı` sayılır.
- `YF_PROXY_FAILURE_THRESHOLD=3` pratikte ~6 başarısız Yahoo isteğine karşılık
  gelir; eşik bu ölçekte seçilmiştir.

### 5.4 Sağlık durum makinesi

Saf bir fonksiyon (DB'siz, tam geçiş tablosuyla test edilebilir):

```python
def apply_outcome(state: ProxyHealthState, event: HealthEvent,
                  now: datetime, policy: ProxyPolicy) -> ProxyHealthState
```

`HealthEvent` = `SUCCESS | RATE_LIMITED | BLOCKED | NETWORK | SHARD_CRASH`.

| Olay | Etki |
|---|---|
| `SUCCESS` | `success_count++`, `last_ok_at=now`, `consecutive_failures=0`, `health='healthy'` |
| `RATE_LIMITED`/`BLOCKED`/`NETWORK` | `failure_count++`, `consecutive_failures++`, `last_error`/`last_error_at` |
| ↑ sonrası `consecutive_failures >= YF_PROXY_FAILURE_THRESHOLD` | `health='cooldown'`, `cooldown_until = now + YF_PROXY_COOLDOWN_SECONDS`, `cooldown_rounds++`, `consecutive_failures=0` |
| **`SHARD_CRASH`** | **Eşikten bağımsız**: doğrudan `health='cooldown'`, `cooldown_until`, `cooldown_rounds++` |
| `cooldown_rounds >= YF_PROXY_DEAD_ROUNDS` | `health='dead'` |
| Cooldown süresi doldu | Uygunluk sorgusuna yeniden girer; **`health` `'cooldown'` olarak KALIR**, yalnızca ilk `SUCCESS` `'healthy'` yapar |
| `DATA` / `UNKNOWN_SYMBOL` | Proxy durumuna **hiç dokunulmaz** |

**`SHARD_CRASH` neden ayrı bir olay.** Parent, ölen bir child'ın proxy'sini
cooldown'a almak ister; ama tek bir `NETWORK` olayı yalnızca sayacı bir artırır
ve child'ın bellekteki sayaçları flush edilmemiş olabileceği için parent bayat
bir durum üzerinde çalışır — proxy çoğu senaryoda cooldown'a hiç girmezdi.

**`SUCCESS` `cooldown_rounds`'u sıfırlamaz** — bilinçli. Sıfırlasaydı arada tek
bir başarı `dead`'e giden yolu sürekli baştan başlatır ve yarı-ölü bir proxy
sonsuza dek havuzda kalırdı (`FAILURE_THRESHOLD=3 × DEAD_ROUNDS=3` ve
`COOLDOWN=900s` ile bir run içinde ikinci tura girmek ≥30 dk sürer). `cooldown_rounds`
yalnızca `yfin proxy reset` ile sıfırlanır.

**`proxy list` çıktısı** cooldown süresi dolmuş proxy'yi `cooldown (expired)`
olarak gösterir; ENUM'un yalan söylemesi böylece operatöre yansımaz.

**Yazma noktası.** Sağlık güncellemesi sembol transaction'ının *içinde olmaz* —
rollback, proxy'nin bandığı bilgisini de silerdi. Child sayaçları bellekte
biriktirir ve şu iki anda **ayrı, kısa** bir transaction'da flush eder:
(a) eşiği geçtiği anda, (b) shard bitiminde. Tek istisna §4.4/7'deki
`SHARD_CRASH`: ölmüş child kendi durumunu flush edemeyeceği için kararı parent
yazar — aynı `apply_outcome` fonksiyonuyla, politika tek yerde kalır.

**Lost update koruması.** `proxies` satırına birden çok yazar biner (child'ın
flush'ı, parent'ın `SHARD_CRASH`'i, eşzamanlı `yfin proxy check`). Kümülatif
sayaçlar bu yüzden **artımlı** yazılır (`SET success_count = success_count + :n`)
ve durum geçişi `SELECT ... FOR UPDATE` altında yapılır. `yfin proxy check` sync
sürerken çalıştırılabilir; kendi kısa transaction'ını kullanır ve advisory lock
almaz.

### 5.5 Aktif kontrol — `yfin proxy check`

Kontrol, `curl_cffi` ile **doğrudan** istek atar (`impersonate="chrome"`);
yfinance kullanılmaz, çünkü `yf.config` process-global olduğu için (§2.1) N
proxy'yi paralel kontrol etmek yine N process gerektirirdi. Ham istek basit bir
thread havuzunda koşar.

**İki endpoint denenir** — biri yetmez:

1. `https://query2.finance.yahoo.com/v8/finance/chart/AAPL?range=1d&interval=1d`
   — crumb istemez (`data.py:442-447` bunu açıkça söylüyor).
2. `https://query1.finance.yahoo.com/v1/test/getcrumb` — gerçek sync trafiği
   **her** istekte buradan geçer (`data.py:268`, `data.py:363`).

Bir proxy chart için 200 dönerken `getcrumb` veya consent akışında bloklanabilir;
tek endpoint'lik bir kontrol onu `healthy` raporlardı.

| Yanıt (herhangi bir endpoint) | Olay |
|---|---|
| İkisi de `200` | `SUCCESS`; `last_latency_ms` (chart), `last_checked_at` |
| `429` | `RATE_LIMITED` |
| `401`/`403` | `BLOCKED` |
| Bağlantı/timeout/proxy hatası | `NETWORK` |
| Parola çözülemedi | Durum **değişmez**; açık hata raporlanır (§3.4) |

**Sınır:** ham istekte cookie jar boştur, gerçek istekler cookie'li gider. Bu
yüzden `check` **tamamlayıcıdır**; birincil sağlık kaynağı pasif gözlemdir
(sync sonuçları).

`reset` komutu `health='unknown'`, `consecutive_failures=0`,
`cooldown_rounds=0`, `cooldown_until=NULL` yapar; kümülatif
`success_count`/`failure_count` **korunur**.

---

## 6. yfinance advanced entegrasyonu

### 6.1 Tek nokta: `configure_yfinance()`

`client.py` içinde, child başlangıcında bir kez (§4.3 adım 4):

| Ayar | Değer | Gerekçe |
|---|---|---|
| `yf.config.network.proxy` | shard'ın DSN'i veya `None` | §2.3 |
| `yf.config.network.retries` | `0` | iç istisna-retry'ını kapatır; §2.5 |
| `yf.config.debug.hide_exceptions` | `False` | §6.2 |
| `yf.config.debug.logging` | `LOG_LEVEL == "DEBUG"` | köprüden **sonra** atanır, §6.5 |
| `yf.set_tz_cache_location(...)` | `YF_TZ_CACHE_DIR/<proxy_key>` | §4.11 |

`yf.set_config()` **kullanılmaz** — 1.7.0'da `DeprecationWarning` üretir ve
yalnızca `proxy`/`retries` alır. Doğrudan `yf.config.*` atanır.

Fonksiyonun son adımı bir **doğrulama**dır: `assert yf.config.debug.hide_exceptions
is False` vb. §2.1'deki sessiz `None` davranışı yüzünden yazım hatası aksi halde
fark edilmezdi.

### 6.2 `hide_exceptions = False` — kapsamı ve kabul edilen bedeli

Bayrak 52 yerde okunur; `scrapers/history.py`, `quote.py`, `fundamentals.py`,
`holders.py`, `funds.py`, `analysis.py`, `base.py`, `search.py`, `lookup.py`
hepsinde aynı desen: `except Exception as e: if not hide_exceptions: raise;
logger.error(...); return None` (örn. `base.py:186-190`).

**Kapsam yalnızca gizli hatalar değildir** — meşru "veri yok" durumları da
fırlar:

- `YFTzMissingError` (`history.py:204`) — delisted sembol
- `YFPricesMissingError` (`history.py:300`) — aralıkta fiyat yok
- `YFInvalidPeriodError`

Bunlar bugün sessizce `empty_df()` dönüyor. **`classify_error` bu üçünü açıkça
`DATA` olarak sınıflandırır** (§5.2) — bu sınıf YALNIZCA proxy sağlığı
içindir. `empty` üretimi `ErrorKind`'dan DEĞİL, uçtaki açık
`client.call_optional` + `errors.is_absent_data` (404 + tek bilinen
`IndexError`) yolundan gelir (AH §8.4); aksi halde `failed` sayısı
kalıcı şişer ve çıkış kodu sürekli `EXIT_PARTIAL` kalır.

Kazanç: T§8.2'nin `empty` ≠ `failed` ayrımı ilk kez gerçekten doğru çalışır.
Bedel açıkça kabul ediliyor: ilk canlı run'ın `failed` kalemleri tek tek
incelenir ve gerçekten "veri yok" olanlar §5.2'deki haritaya eklenir.

İki upstream tuzağı: `quote.py:539-542` bayraktan bağımsız olarak da fırlatır
(upstream tutarsızlığı, bilinmesi yeterli); ve `history(..., raise_errors=True)`
**kullanılmaz** — `history.py:155` `DeprecationWarning` üretir, yalnızca
`hide_exceptions` kullanılır.

### 6.3 `history(repair=True)`

Doğrulandı: parametre `scrapers/history.py:107`'de var; kolon adı tam olarak
`"Repaired?"`; `repair=True` **ve çerçeve boş değilse** kolon garanti edilir
(`history.py:560-563`), boş çerçevede ve `repair=False` iken **yoktur**;
onarım ek ağ isteği atar (`history.py:987` daha küçük interval, `history.py:1282`
FX çevrimi; özyineleme derinliği 2 ile sınırlı); ve yalnız `1d`'de güvenilirdir
(`history.py:166-170`) — bizim kullandığımız interval (T§2).

**Uygulama — spec'in ilk taslağındaki tarif hatalıydı, düzeltildi:**

- `is_repaired` **`_COLUMN_MAP`'e KONMAZ.** Konsaydı `normalize`'ın jenerik
  döngüsü (history.py:111-121) onu `else` dalında `nz.to_decimal` ile işler ve
  BOOLEAN kolona `Decimal`/`None` yazardı.
- Bunun yerine sabit `row` sözlüğüne doğrudan yazılır:
  `"is_repaired": nz.to_bool(record.get("Repaired?")) or False`.
  Böylece **her satırda mevcut olur** ve `present` filtresine
  (history.py:89) hiç girmez. Girseydi kolon gelmediğinde satırdan düşer,
  ardından `MySQLRowWriter.write`'taki `present = set(rows[0])` kesişimi
  (persistence.py:94-95) onu `ON DUPLICATE KEY UPDATE` kapsamından da düşürürdü.
- **Salınım problemi ve çözümü.** yfinance'in repair heuristikleri **pencere
  uzunluğuna bağlıdır**. Artımlı çekim `start = watermark −
  YF_INCREMENTAL_OVERLAP_DAYS` ile dar bir pencere kullanır; aynı
  `(symbol, session_date)` satırı bir run'da `is_repaired=1`, ertesi run'da
  `0` gelebilir ve `UPDATE_COLUMNS`'ta olduğu için geri yazılırdı — kolonun
  denetim değeri sıfır olurdu. Ayrıca `YF_HISTORY_REPAIR=false`'a dönüldüğünde
  tüm geçmiş işaretler `0`'a ezilirdi.
  **Çözüm:** `TableWrite`'a `monotonic_columns: tuple[str, ...]` alanı eklenir;
  `MySQLRowWriter` bu kolonlar için `ON DUPLICATE KEY UPDATE
  is_repaired = GREATEST(is_repaired, VALUES(is_repaired))` üretir. Değer
  yalnızca `0 → 1` yönünde ilerler. Bu, T§7.2 idempotency ilkesinin
  ("tekrar çalıştırma veri kaybettirmez") doğrudan uygulanmasıdır.

`price_history`'de `content_hash` yoktur, dolayısıyla snapshot hash mantığı
etkilenmez.


**`[repair]` ekstrası ZORUNLUDUR — canlı koşuda doğrulandı.** yfinance onarım
heuristiklerinde `scipy.ndimage` (`scrapers/history.py:1338`) ve
`sklearn.cluster.DBSCAN` (`scrapers/history.py:820`) modüllerini **tembel
import** eder ve bunları `yfinance[repair]` ekstrasında tutar (`scipy>=1.6.3`,
`scikit-learn>=1.0`). Ekstra kurulu değilse çağrı `ModuleNotFoundError` ile
düşer — ve paylaşılan çerçeve yüzünden bu, **her sembolde `history` +
`dividends` + `splits` + `capital_gains` hücrelerini birden** başarısız yapar,
yani `price_history` hiç yazılmaz. İlk canlı `--full-refresh` koşusunda tam
olarak bu gerçekleşti: 9/9 sembol, 36 başarısız hücre, sıfır fiyat satırı.

İki önlem alınır: (a) bağımlılık `yfinance[repair]>=1.7.0` olarak beyan edilir;
(b) `repair_enabled()` ekstranın varlığını process başına bir kez ölçer, eksikse
onarımı kapatır ve durumu **bir kez GÖRÜNÜR şekilde** loglar. Toplam veri
kesintisi yerine onarımsız ama çalışan bir koşu tercih edilir — cron'a bağlı bir
hat için doğru davranış budur.

**İşletme notu:** onarım geçmişi geriye dönük değiştirebilir; 7 günlük artımlı
pencere bunu yakalamaz. Düzeltmelerin tamamının inmesi için **periyodik
`--full-refresh` gerekir**. Bu bir kusur değil, `README` ve `yfin sync --help`
metnine giren bir işletme notudur.

### 6.4 `dividends`/`splits`/`capital_gains` onarılmış çerçeveden beslenir

**T§6.3'ün "bağımsız çağrı" kararı geri alınır.** Gerekçe ölçüldü:

- `base.py:479-486` `repair` parametresini `PriceHistory.get_dividends`'e
  **forward etmez**; `ticker.dividends` → sabit `repair=False`.
- `history.py:646` önbellek anahtarı `(interval, period, repair)`'dır. Yani
  `ticker.dividends`, `history(repair=True)`'nin çerçevesinden **farklı bir
  cache girdisi** kullanır: hem **onarımsız** veri döner hem de **ikinci bir tam
  `history()` ağ çağrısı** yapar.

Sonuç: bugünkü tasarımda `price_history.dividend` (onarılmış) ile `dividends`
tablosu (onarımsız) **çelişirdi** — üstelik T§5.2'ye göre otorite olan taraf
`dividends` tablosudur.

**Değişiklik.** `corporate_actions.py`'deki `_SeriesDataset.fetch`,
`get_dividends`/`get_splits`/`get_capital_gains` yerine
`fetch_history_frame(ctx)` çağırır ve `normalize` çerçevenin
`Dividends` / `Stock Splits` / `Capital Gains` **kolonlarını** okur
(`actions=True` şart; `history.py:619-620` aksi halde bu kolonları düşürür).
Mevcut anahtar-bazında dedupe ve `nz.to_local_date` mantığı korunur; kolon
yoksa sonuç `empty`'dir (fon olmayan sembolde `Capital Gains` gibi).

Üç kazanç: otorite tablolar onarılmış veri alır; `price_history.dividend` ile
`dividends` arasındaki çelişki ortadan kalkar; **sembol başına üç ağ çağrısı
kaybolur** (rate-limit bütçesi rahatlar — §5.3'teki 2× çarpanıyla birlikte
kayda değer).

**Paylaşılan çerçevenin `start`'ı.** `fetch_history_frame` bugün yalnızca
`price_history.session_date` watermark'ına bakıyor. Üç tabloyu daha beslediği
için kural genişler: çerçevenin `start`'ı, **çalıştırmada seçili olan** tüketici
tabloların watermark'larının **minimumudur**; herhangi biri yoksa
`period="max"` kullanılır. Aksi halde `price_history` güncel ama `dividends`
boşken artımlı pencere eski temettüleri kaçırırdı.

**`depends_on` değişmez** (`("symbols",)`). Üç dataset `fetch_history_frame`'i
doğrudan çağırır; `history` de seçiliyse `ctx.cached(CACHE_HISTORY)` çağrıyı
paylaşır. `depends_on = ("history",)` yapmak `--datasets dividends`
çalıştırmasının `price_history`'ye de yazmasına yol açardı — istenmeyen bir yan
etki.

### 6.5 Logging köprüsü

`logging.getLogger("yfinance")` structlog'a köprülenir. **Sıra bağlayıcıdır:**
köprü handler'ı `yf.config.debug.logging = True` atamasından **önce** eklenir.
Sebep: bu atama `_enable_debug_mode()`'u tetikler (`utils.py:184-185, 199-212`)
ve yfinance, logger'da **hiç handler yoksa** kendi `StreamHandler`'ını ekleyip
seviyeyi `DEBUG`'a zorlar — her satır iki kez basılırdı (biri structlog
formatında, biri `%(levelname)-8s %(message)s` ile stderr'e). Köprü önce
bağlanırsa `len(handlers) > 0` olur ve yfinance kendi handler'ını eklemez.

**Alan bağlaması `contextvars` ile YAPILMAZ.** `structlog.contextvars`
değerleri `ThreadPoolExecutor` worker thread'lerine kopyalanmaz (executor
`copy_context()` kullanmaz), ve fetch/normalize ile yfinance'in kendi logları
tam olarak o thread'lerde üretilir (runner.py:353) — `proxy_label`/`shard_index`
asıl ihtiyaç duyulan satırlarda boş kalırdı. Bunun yerine
`ThreadPoolExecutor(initializer=..., initargs=(run_id, shard_index, proxy_label))`
kullanılır ve her worker thread kendi bağlamını başlangıçta bağlar.

Köprüye **zorunlu redaction processor** eklenir (§3.4): proxy DSN deseni
`scheme://user:***@host:port`'a indirgenir.

---

## 7. Yapılandırma

### 7.1 Yeni `.env` anahtarları

```
YF_MAX_SHARDS=4                  # ge=1
YF_SHARD_TIMEOUT_SECONDS=3600    # ge=60; aşan child SHARD_CRASH sayılır
YF_PROXY_COOLDOWN_SECONDS=900    # ge=1
YF_PROXY_FAILURE_THRESHOLD=3     # ardışık hata → cooldown (ge=1)
YF_PROXY_DEAD_ROUNDS=3           # kümülatif cooldown turu → dead (ge=1)
YF_PROXY_SECRET_KEY=             # Fernet anahtarı; boşsa parolalı proxy eklenemez
YF_PROXY_CHECK_TIMEOUT=10        # saniye (gt=0)
YF_TZ_CACHE_DIR=.cache/yfinance  # proxy başına alt dizin açılır
YF_HISTORY_REPAIR=true
YF_TXN_RETRY_ATTEMPTS=3          # deadlock/lock-timeout yeniden denemesi (ge=1)
```

`.gitignore`'a `.cache/` eklenir.

### 7.2 Mevcut anahtarların değişen anlamı

- **`YF_RATE_LIMIT_PER_SEC` artık shard başınadır.** Toplam efektif hız
  `shard_count × YF_RATE_LIMIT_PER_SEC`'tir. Bu, her shard'ın ayrı bir çıkış
  IP'si kullanması nedeniyle **istenen** semantiktir. İki uyarı: (a) `--no-proxy`
  ile `--shards N` birlikte verilirse N zorla 1'e iner (§4.8), aksi halde aynı
  IP'den N kat hız çıkardı; (b) §2.5'teki cookie-retry çarpanı ve §6.3'teki
  onarım istekleri gerçek istek sayısını nominalin üstüne taşır.
- **`YF_QUEUE_MAXSIZE` shard başınadır.** Toplam bellek tavanı
  `shard_count × YF_QUEUE_MAXSIZE` normalize edilmiş sembol payload'ıdır
  (`^GSPC` tek başına 24 786 satır). 4 shard'da bugünkünün 4 katı.
- **`YF_MAX_WORKERS` shard başınadır.** Toplam thread `shard_count ×
  YF_MAX_WORKERS`. Shard başına bucket 2 rps olduğu için 4 thread bucket'ı
  doyurmaya fazlasıyla yeter; artırmak hız kazandırmaz.
- Sembol kuyruğu (`mp.Queue`) sınırsızdır: yalnızca kısa string taşır.

### 7.3 Bağlantı invariant'ı

`(shard_count × 5) + 2 ≤ MySQL max_connections` (§4.12). Varsayılanlarla
`4×5+2 = 22`, `max_connections=151` içinde rahat.

---

## 8. CLI

```bash
yfin proxy add socks5h://user:pass@host:1080 --label eu-1
yfin proxy list [--all]        # label, scheme, host:port, enabled, health,
                               # cooldown, ok/fail, latency   (parola YOK)
yfin proxy enable LABEL
yfin proxy disable LABEL
yfin proxy reset LABEL         # health='unknown', sayaçlar 0 (kümülatifler korunur)
yfin proxy remove LABEL
yfin proxy check [--label L]

yfin sync --shards N           # YF_MAX_SHARDS'ı geçici olarak ezer
yfin sync --no-proxy           # havuzu yok say; tek shard, doğrudan bağlantı
yfin sync --require-proxy      # uygun proxy yoksa çalışma
```

`add` DSN'i parse eder, parolayı Fernet ile şifreler, `label` verilmemişse
`host-port`'tan türetir. `https://` şeması verilirse curl_cffi'nin
`CurlCffiWarning`'i yansıtılır (kullanıcı büyük olasılıkla CONNECT-tünel için
`http://` istiyor).

### 8.1 Çıkış kodları

T§8.1'deki kodlar korunur; **bir yeni kod eklenir** ve haritalama netleştirilir:

| Kod | Ad | Koşul |
|---|---|---|
| 0 | `EXIT_OK` | `failed` **ve** `not_attempted` yok |
| 1 | `EXIT_NO_SYMBOL_RESOLVED` | sembol var, hiçbiri çözülemedi |
| 2 | `EXIT_PARTIAL` | `failed` var **veya** `not_attempted` var |
| 3 | `EXIT_ALL_FAILED` | `unknown_symbol` dışı tüm hücreler `failed` |
| 4 | `EXIT_LOCK_NOT_ACQUIRED` | advisory lock alınamadı |
| **5** | **`EXIT_NO_PROXY`** | `--require-proxy` verildi, uygun proxy yok |

**İşlenmemiş sembol varken çıkış kodu asla 0 olamaz** — §4.7'nin sessiz veri
kaybı senaryosunu kapatan kural budur. "Hiç shard kalmadı" hali ayrı bir kod
almaz: hücre bazlı kurallar (2 veya 3) zaten doğru sonucu verir.

---

## 9. Migration

Tek bir Alembic revizyonu (tek initial migration `5cd520a87bb0` üstüne biner):

1. `proxies` tablosu + `UNIQUE(label)`, `UNIQUE(scheme,host,port,username)`,
   `ix_proxies_eligibility`
2. `sync_runs.shard_count`
3. `sync_run_items.shard_index`, `proxy_id`, `proxy_label`, `ix_sync_run_items_proxy`
4. `sync_run_items.status` ENUM'una `'not_attempted'` eklenmesi — **elle
   yazılır**, Alembic autogenerate ENUM değer değişikliklerini yakalamaz
5. `price_history.is_repaired`

Hepsi varsayılan değerli veya nullable olduğu için mevcut veri üzerinde geriye
dönük dolgu gerektirmez. `downgrade()` kolonları ve tabloyu düşürür, ENUM'u eski
haline alır.

---

## 10. Test stratejisi

T§9'daki dört katman korunur.

### 10.1 Unit (ağsız, DB'siz)

- DSN parse + redaction (parola `repr`, log ve hata metninde sızmaz)
- Fernet round-trip; anahtar yokken açık hata; anahtar değişince "çözülemedi"
- **Sağlık durum makinesi — tam geçiş tablosu:** `SUCCESS`, üç hata sınıfı,
  `SHARD_CRASH`, eşik aşımı, cooldown süre dolumu (health `'cooldown'` kalır),
  `dead`'e geçiş, `SUCCESS`'in `cooldown_rounds`'u sıfırlamaması,
  `DATA`/`UNKNOWN_SYMBOL`'ün hiçbir şeyi değiştirmemesi
- **`classify_error` vaka tablosu**, özellikle sıra: `HTTPError(403)`'ün
  `BLOCKED` olması (`OSError` türevi olmasına rağmen `NETWORK` olmaması) ve
  `YFPricesMissingError`'ın `DATA` olması
- `SYMBOL_DATASETS.resolve(names)` çıktısının parent ve child'da birebir aynı
  olması (kayıt sırasından bağımsız — §4.2)
- Shard sayısı formülü (§4.4/3) ve `--no-proxy` + `--shards N` etkileşimi
- `is_repaired` monotonluğu: `GREATEST` semantiğinin `1 → 0` yazmaması

### 10.2 Repo (gerçek MySQL, ağsız)

- `proxies` CRUD + uygunluk sorgusunun tam doğruluk tablosu
  (`is_enabled` × `health` × `cooldown_until` kombinasyonları), sıralamada
  `unknown`'ın konumu
- **Eşzamanlılık testleri `db_session` fixture'ıyla yazılamaz.** O fixture tek
  bağlantıda açılıp sonunda rollback edilen bir transaction'dır
  (`tests/conftest.py:51-65`); ikinci session ilkinin yazdığını göremez ve
  deadlock hiç oluşmaz. Bu testler `test_engine` üzerinden **iki bağımsız
  `Session`** kurar, gerçekten commit eder ve `try/finally` ile kendi satırlarını
  siler (`test_engine` session-scope'tur, sızan satır diğer testleri etkiler).
  Bunun için conftest'e `committed_session` fixture'ı eklenir.
  - Aynı `news_id` üzerinde eşzamanlı upsert → §4.10 retry'ının çalıştığı
  - `news` / `news_symbols` FK'sı üzerinden çapraz deadlock
- **Alembic drift testi:** temiz şemada `alembic upgrade head` koşulur, ardından
  `compare_metadata(MigrationContext, Base.metadata)` **boş** dönmelidir
  (`migrations/env.py:22-31`'deki `include_object` filtresi aynen kullanılarak).
  Bugün testler şemayı `Base.metadata.create_all()` ile kuruyor
  (`conftest.py:39-46`), yani migration ile model arasındaki drift hiçbir yerde
  yakalanmıyor; §9'un yanlış yazılması canlıda `ERROR 3780`/`1054` olarak
  patlardı.
- 2 shard'lı koordinatör (sahte dataset'lerle): iddia **"her sembol tam olarak
  bir kez işlendi, `run_id` ortak, `shard_index` geçerli aralıkta"** olmalıdır —
  "semboller bölüşüldü" iddiası dinamik kuyrukla garanti değildir ve test
  rastgele kırılır. Bölüşmeyi görmek için yapay gecikmeli sahte dataset'le ayrı
  bir test yazılır.
- `open_run`'ın commit'i ile child'ın ilk item yazımı arasındaki sıralama
- İşlenmemiş sembollerin `not_attempted` yazıldığı ve çıkış kodunun 0 olmadığı

### 10.3 Live (`-m live`)

- `yfin proxy check` gerçek proxy ile (iki endpoint)
- 2 shard'lı gerçek sync; `is_repaired` kolonunun dolduğu
- `hide_exceptions=False` altında `failed` kalemlerinin incelenmesi (§6.2)
- `dividends` tablosunun `price_history.dividend` ile tutarlı olduğu (§6.4)

### 10.4 Statik analiz

`ruff` + `mypy --strict` mevcut ayarlarla geçmeli. `multiprocessing` sınırından
geçen tüm veriler tiplenir (`ShardSpec`, `ShardCounters` dataclass'ları,
`__main__` dışında bir modülde).

---

## 11. Bağımlılıklar

`cryptography` (Fernet) zaten doğrudan bağımlılıktır. `multiprocessing` standart
kütüphanededir.

**Değişiklik 1 — `yfinance` → `yfinance[repair]>=1.7.0`.** Gerekçesi §6.3'te:
onarım `scipy` ve `scikit-learn`'e ihtiyaç duyar ve eksiklikleri sessiz değil
**tam** bir veri kesintisi üretir. İkisi `mypy` override listesine de eklenir
(`scipy.*`, `sklearn.*` py.typed taşımaz).

**Tek değişiklik:** `curl_cffi>=0.15` `pyproject.toml`'a **açık** bağımlılık
olarak eklenir. Bugün yalnızca `yfinance` üzerinden geçişli olarak kuruludur
(mypy override'ı `pyproject.toml:54`'te var ama `dependencies`'te yok); §5.5 onu
doğrudan import ettiği için geçişli bir bağımlılığa yaslanmak kırılgan olur —
üstelik `_http.py` `YF_DISABLE_CURL_CFFI` ile onsuz da çalışabildiğini gösteriyor.
Buna rağmen import başarısız olursa `yfin proxy check` düz `requests` ile çalışır
ve çıktısında TLS taklidi olmadığını uyarı olarak belirtir.

---

## Ek A — Karar özeti

| Karar | Seçim | Gerekçe |
|---|---|---|
| Rotasyon ekseni | Process (shard) | `yf.config` ve `YfData` process-global (§2.1–2.4) |
| Shard ↔ proxy | **Shard başına bir proxy**; `shard_count = max(1, min(N, \|eligible\|))` | Rate limit, cooldown ve ban sayımı tek çıkış IP'sine karşılık gelir (§4.4) |
| Proxy yoksa | Tek shard, doğrudan bağlantı; havuz doluyken hiçbiri uygun değilse WARNING | Geriye uyumlu; sessiz ban riski alınmaz (§4.8) |
| `--no-proxy` | Shard zorla 1; proxy sağlığı **hiç** güncellenmez | Kullanıcı bilinçli baypas ediyor (§4.8) |
| Dağıtım | Dinamik `mp.Queue` | Straggler yok; ölen shard'ın işi kalanlara geçer |
| Cooldown'a giren shard | Kuyruktan çekmeyi **bırakır**, temiz çıkar | Aksi halde banlanmış proxy en çok sembolü çeker (§4.7) |
| İşlenmemiş sembol | `not_attempted` satırı; çıkış kodu asla 0 değil | Sessiz veri kaybını kapatır (§4.7, §8.1) |
| Sembol düzeyinde retry | **Yok**; yeniden deneme run düzeyinde | Tek transaction ve denetim tekilliği (§4.6) |
| Toplamların kaynağı | **DB** (`sync_run_items` agregasyonu) | Çift sayım imkânsızlaşır; `ShardCounters` yetkili değil (§4.5) |
| `runner.py` | `open_run` / `run_shard` / `finalize_run` olarak ayrılır | Parent/child sorumluluk ayrımı (§4.1) |
| Start method | `spawn`, açıkça | Linux taşınabilirliği; fork'ta soket/CFFI handle bozuk (§4.2) |
| Child'a geçen dataset | Adlar (`list[str]`) | Determinizm `Registry._expand`'ten gelir, kayıt sırasından değil (§4.2) |
| İstisnalar | Kuyruğa **ham nesne konmaz**, `(ErrorKind, str)` | `YFTickerMissingError` pickle round-trip'te `TypeError` verir (§4.2) |
| Child'ın ilk adımı | `configure_logging` | `cache_logger_on_first_use=True` (§4.3) |
| Aktiflik | `is_enabled` + `health` ayrı | Operatör kararı ile sistem gözlemi karışmasın (§3.2) |
| `username` | `NOT NULL DEFAULT ''` | MySQL UNIQUE, NULL'lu satırlarda tekilliği zorlamaz (§3.3) |
| Parola | Fernet, `.env` anahtarı + köprüde redaction | DB dökümü tek başına kullanılamaz; upstream logları da redakte edilir (§3.4) |
| `proxy_id` | **FK YOK** + `proxy_label` kopyası | FK'nın S-lock'u sağlık flush'ının X-lock'uyla deadlock üretir (§3.5) |
| Sağlık kararı | Pasif (sync) birincil + aktif (`check`) tamamlayıcı | `check` cookie'siz ve dar kapsamlı (§5.5) |
| `proxy check` endpoint'i | chart **ve** `getcrumb` | Gerçek trafik ikisinden de geçer (§5.5) |
| Proxy sağlığını etkileyen hata | Yalnız `RATE_LIMITED`/`BLOCKED`/`NETWORK` | Geçersiz sembol proxy'yi cezalandırmasın (§5.2) |
| `classify_error` sırası | HTTP status **önce**, `OSError` sonra | `curl_cffi` `RequestException` `OSError` türevi (§5.2) |
| `BLOCKED` | Retry **edilmez** | Cooldown zaten devreye giriyor; 30 sn ve bucket boşa gider (§5.2) |
| `SUCCESS` | `cooldown_rounds`'u sıfırlamaz | Aksi halde `dead`'e hiç ulaşılmaz (§5.4) |
| `SHARD_CRASH` | Eşikten bağımsız ayrı olay | Tek `NETWORK` olayı cooldown üretmezdi (§5.4) |
| Sağlık yazımı | Sembol transaction'ının **dışında**; artımlı `UPDATE` + `FOR UPDATE` | Rollback ban bilgisini silmesin; lost update olmasın (§5.4) |
| tz/cookie/ISIN cache | Dizin **proxy kimliğiyle** anahtarlanır | `shard_index` run'lar arası başka proxy'ye kayar; cookie PK'sı `strategy` (§4.11) |
| Bağlantı havuzu | Child `pool_size=3, max_overflow=2` | Gerçek eşzamanlı tüketici 2; invariant `(N×5)+2 ≤ max_connections` (§4.12) |
| `retries` | `0`'a sabitlenir | İç istisna-retry'ını kapatır; ama cookie-retry kapatılamaz, 2× çarpan (§2.5) |
| `hide_exceptions` | `False` + `call_optional` (AH §8.4) | `empty` ≠ `failed` ilk kez doğru çalışsın (§6.2) |
| `repair` | Açık, varsayılan `true`; `is_repaired` **monotonik** | Dar pencerede salınımı ve `false`'a dönüşte ezilmeyi önler (§6.3) |
| `[repair]` ekstrası | Bağımlılığa eklenir + çalışma anında kontrol | Eksikse onarım HER sembolde patlar, `price_history` hiç yazılmaz (§6.3) |
| `dividends`/`splits`/`capital_gains` | **Onarılmış çerçevenin kolonlarından** | `ticker.dividends` `repair`'i forward etmez, ayrı cache + ayrı çağrı (§6.4) |
| Log alan bağlaması | `ThreadPoolExecutor(initializer=...)` | `contextvars` worker thread'lere kopyalanmaz (§6.5) |
| Log köprüsü sırası | Köprü **önce**, `debug.logging=True` sonra | Yoksa yfinance kendi handler'ını ekler, çift çıktı (§6.5) |
| İptal | Sinyal protokolü; lock child'lardan sonra bırakılır | Orphan child + serbest kalan lock = üst üste binen sync (§4.9) |
| `news` çift sayımı | `sync_runs` toplamları bu iki tablo için **üst sınır** | `_verify` yazarı ayırt etmez (§4.10) |
| `locale` | Kapsam dışı | Process-global; sembol başına locale shard eşlemesiyle çatışır |
| `MARKET_DATASETS` | Shard dışı, ama **proxy'li** (tek proxy) | Sembol eksenli değil; kuyruğa girseydi N kez çekilirdi (§4.2) |
| `ErrorKind` konumu | `client.py` değil `errors.py` | Proxy alan modeli yfinance sarmalayıcısına bağımlı olmasın (§4.1) |
| `proxy` | Tek dosya değil paket | Saf durum makinesi DB ve ağ I/O'sundan ayrılsın (§4.1) |
| Uygunluk | **Yalnızca SQL**; Python ikizi kaldırıldı | İki kaynak birbirinden sapabilirdi (§5.1) |
