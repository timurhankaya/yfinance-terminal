# Salt-okuma Veri API'si + OAuth2 client_credentials — Tasarim

Tarih: 2026-09-06
Durum: onaylandi (3 bagimsiz inceleme sonrasi revize edildi)

## 1. Amac ve kapsam

`yfin` veri hattinin PostgreSQL 18 + TimescaleDB'de biriktirdigi veriyi
HTTP uzerinden disariya acan, **salt-okuma** bir API. Kimlik dogrulama
client id / client secret ile OAuth2 `client_credentials` akisi uzerinden
yapilir. Sozlesme OpenAPI olarak yayimlanir ve CI'da kilitlenir.

Bu spec ayrica `src/yfin/` paket duzeninin katmanli bir yapiya
tasinmasini kapsar; `api/` paketi ayni katman dilini tekrarlar.

**Kapsam icindeki dis yuzey:** okuma uc noktalari, OpenAPI sozlesmesi,
token akisi, scope, hiz limiti **ve kota UYGULAMASI**, kullanim olcumu
(`api_usage_daily`), istemcilerin CLI ile yonetimi.

### Kapsam disi (ayri alt sistemler, ayri spec)

Hedef "halka acik / self-servis SaaS"tir, ama uc bagimsiz alt sisteme
ayrilir ve bu spec yalnizca birincisini kapsar:

1. **Veri API'si** (bu spec). Istemciler `yfin api client create` ile
   ELLE olusturulur.
2. **Self-servis kayit** — kaydolma, e-posta dogrulama, gelistirici
   portali, secret rotasyonunun kullanici arayuzu.
3. **Faturalama** — fiyatlandirma, plan yukseltme, odeme saglayici.
   Kotanin OLCULMESI ve UYGULANMASI bu spec'tedir; PARAYA cevrilmesi
   degil.

2 ve 3'un bu tasarima dokunacagi iki nokta simdiden hazirlanir:
`api/storage` deposu CLI'dan bagimsizdir (portal ayni depoyu cagirir) ve
`api_usage_daily` tablosu bugunden doldurulur.

Veri hattini YONETEN uc noktalar (sync tetikleme, proxy/ayar yonetimi)
kapsam disidir. API veri yazmaz.

## 2. Mimari kararlar

### K1 — Senkron, ayni pakette

API `src/yfin/api/` altinda yasar, mevcut `create_db_engine` ve
SQLAlchemy modellerini kullanir, uc nokta fonksiyonlari `def`'tir ve
uvicorn onlari thread havuzunda kosturur.

Gerekce: kod tabani bastan sona senkron, `mypy --strict` altinda ve yogun
belgelenmis. Yeni bir alt sistemi ikinci bir deyimde (async) yazmak iki
paralel idiom dogururdu; darbogaz ise event loop degil PostgreSQL. Async'e
gecis kapisi acik birakilir: TUM SQL `api/storage` icinde durur.

**Bu kararin bedeli §5.5'te kapatilir.** Thread havuzu sabit boyutludur;
tek bir yavas sorgu bir slotu tutar. Sorgu maliyeti tavanlari (tarih
araligi, `statement_timeout`, eszamanlilik semaforu) bu modelde
opsiyonel bir iyilestirme degil, kesintiyi onleyen zorunlu parcadir.

Reddedilen: (B) async SQLAlchemy + psycopg3 async — bugun olmayan bir yuk
profili icin bugun odenen karmasiklik. (C) ayri paket/deploy — tek repo ve
tek yayim takvimi icin saf ek yuk.

### K2 — OAuth2 client_credentials + kisa omurlu JWT

`POST /oauth/token` id+secret alir, 15 dakika omurlu HS256 imzali JWT
verir. Dogrulama veritabanina gitmez; yetki degisikliklerinin aninda
yansimasi §4.5'teki epoch mekanizmasiyla saglanir.

Reddedilen: opak token (her istekte depo okumasi), her istekte HTTP Basic
(sir her istekte aga cikar, her istekte Argon2 maliyeti).

### K3 — Hibrit uc nokta yuzeyi

Cok kullanilan avuc dolusu kaynak elle tasarlanir; kalan onlarca dataset
registry'den turetilen tek bir genel kalipla acilir (§5.4).

### K4 — Redis, sayac ve iptal icin

Rate limit sayaclari, kota, yetki epoch'u ve "son kullanim" tamponu
Redis'te. `docker-compose.yml`'ye sabit surumlu bir servis olarak eklenir.

### K5 — Kod once, sozlesme CI'da kilitli

FastAPI + Pydantic semalari tek dogruluk kaynagi; `openapi.json` depoda
tutulur, CI uretilenle diff'ler ve fark varsa build kirilir.

**Onkosul:** depoda BUGUN hicbir CI yapilandirmasi yoktur (`.github/`,
`Makefile`, `.pre-commit-config.yaml` yok). Bu karar bir CI iskeleti
kurulmadan uygulanamaz; §9'un 0. adimi budur.

## 3. Paket duzeni

### 3.1 `src/yfin/` yeniden yapilandirmasi

Bugun kokte 19 duz modul + `__init__.py` var. Katmanlar gercek import
yonunden cikarildi:

```
src/yfin/
  __init__.py  (yerinde kalir: yalnizca __version__)
  core/        config.py  errors.py  logging_setup.py  normalize.py
  models/      (degismiyor)
  storage/     db.py  persistence.py  rescale.py  settings_store.py
               contracts.py (YENI)
  ingest/      client.py  screens.py
  datasets/    (degismiyor)
  pipeline/    runner.py  market_runner.py  domain_runner.py  shard.py
               domain_audit.py  prune.py
  proxy/       (degismiyor)
  cli/         app.py (eski cli.py)  bars.py  settings.py (eski cli_config.py)
  api/         (YENI, 3.2)
```

Iki yerlestirme incelemede duzeltildi:

- **`prune.py` `storage/`'a DEGIL `pipeline/`'a gider.** `prune.py:94`,
  `:134`, `:441` fonksiyon ici `from yfin.datasets import ...` yapiyor;
  `storage/`'a konsaydi `storage -> datasets -> storage` dongusu
  surerdi. Budama bir depolama mekanigi degil, kosan bir istir.
- **`cli_config.py` -> `cli/settings.py`.** `cli/config.py` adi
  `core/config.py` ile karisirdi.

#### `storage/contracts.py`

Bugun `datasets/base.py` ile `persistence.py` karsilikli import ediyor;
dongu yalnizca `TYPE_CHECKING` (`base.py:16-17`) ve fonksiyon ici import
(`base.py:263`) ile kiriliyor. Notr module tasinacaklar:

- `TableWrite`, `WriteStats`
- `RowWriter` **ve uc taban protokolu**: `RowSink`, `HashReader`,
  `SymbolLookup` (`persistence.py:71` dordunu birlestiriyor)
- `apply_write` (`persistence.py:80`) — **bu olmadan dongu kapanmaz**:
  `Dataset.upsert` onu CALISMA ZAMANINDA cagiriyor, yani yalnizca
  protokolleri tasimak fonksiyon ici import'u yerinde birakirdi.

#### Kapatilmayan iki dongu (bilinerek)

Bunlar bu tasimanin kapsami disidir ve **oldugu gibi kalir**; spec'in
bunlari cozdugu iddia edilmemelidir:

- `core/config.py` <-> `storage/settings_store.py`: `config.py:411`
  fonksiyon ici `from yfin.settings_store import load_overrides`,
  `settings_store.py:30` modul seviyesinde `from yfin.config import ...`.
  Yani `core` katmani `storage`'a yukari dogru bagimli kalir.
- `cli/app.py` <-> `cli/bars.py`: `cli_bars.py:24-27` fonksiyon ici
  `from yfin.cli import _session_factory` (kaynak yorumda "dairesel
  import olusur" diye yazili).

Giris noktasi `yfin = "yfin.cli.app:main"` olur.

**Kabul olcutu:** tasima sonrasi test takimi, testlerin ICERIGI
degismeden — yalnizca import yollari guncellenerek — yesil kalir. Yesil
degilse tasima bir davranis degistirmistir; API kodu baslamadan once
durulur.

### 3.2 `api/` paketi

```
src/yfin/api/
  app.py            FastAPI uygulamasi: router montaji, middleware,
                    hata isleyicileri, OpenAPI ozellestirmesi
  core/             YFAPI_ onekli BaseSettings, problem-details tipleri,
                    guvenlik basliklari ve proxy guveni middleware'i
  models/           api_clients, api_client_secrets, api_client_scopes,
                    api_plans, api_usage_daily
  storage/          salt-okuma depo katmani — TUM SQL burada
  schemas/          Pydantic yanit modelleri (OpenAPI semalarinin kaynagi)
  routers/          oauth.py  meta.py
                    v1/{symbols,bars,actions,financials,datasets}.py
  auth/             hashing (Argon2id), jwt, Bearer bagimliligi, scope
  ratelimit/        Redis token bucket, kota, semafor, plan politikasi
```

`meta.py`: `/health`, `/health/ready` ve `/v1/datasets` katalogu.
Ayri bir `info.py` router'i YOKTUR — `ticker_info` verisi
`/v1/symbols/{symbol}` yanitinin parcasidir (§5.1).

Katman kurali: `routers/` sorgu yazmaz, `storage/` HTTP bilmez,
`schemas/` SQLAlchemy import etmez.

## 4. Istemci modeli ve kimlik dogrulama

### 4.1 Tablolar

`migrations/versions/` altinda su an TEK revizyon var
(`20260906_0958_initial_postgres_timescaledb.py`, `revision =
'e658b870d8f3'`, `down_revision = None`). Yeni revizyon
`down_revision = 'e658b870d8f3'` alir ve `alembic.ini`'deki
`file_template` (`YYYYMMDD_HHMM_slug`) bicimine uyar.

- `api_clients` — `client_id` (PK), `name`, `owner_email`,
  `plan` (FK -> `api_plans.plan`), `is_active`, `auth_epoch`
  (`INTEGER NOT NULL DEFAULT 0`), `last_used_at`, `created_at`,
  `disabled_at`.
- `api_client_secrets` — `id`, `client_id` (FK), `secret_hash`,
  `created_at`, `expires_at`, `revoked_at`.
- `api_client_scopes` — `client_id` (FK) + `scope` (ENUM).
- `api_plans` — `plan` (PK), `requests_per_second`, `burst`,
  `monthly_quota`, `max_page_size`, `max_concurrency`.
- `api_usage_daily` — `client_id`, `day`, `endpoint_family`,
  `request_count`, `estimated` (bool, §6.4).

`client_id` uretimi: `"yfc_" + secrets.token_urlsafe(24)` — toplam 36
karakter, alfabe `[A-Za-z0-9_-]`. Onek, sizan bir dizenin ne oldugunun
loglardan taninmasi icindir.

`owner_email` **PII'dir**: hicbir `/v1` yanitinda, hata govdesinde veya
log satirinda gorunmez; yalnizca CLI ciktisinda okunur.

**Secret neden ayri tabloda:** rotasyon kesintisiz olmali. Bir istemci
ayni anda en fazla IKI gecerli secret tasir; yenisi verilir, eskisine son
kullanma konur, istemci gecisi kendi takviminde yapar. Kural `rotate`
davranisi olarak uygulanir: iki gecerli secret varken `rotate` **409**
doner, once eskisi revoke edilmelidir. DB tarafinda kismi unique index
ile desteklenir.

**Plan limitleri neden tabloda:** bir planin limitini degistirmek deploy
gerektirmemeli — `settings_store` karariyla ayni gerekce. Limitler surec
icinde 60 saniyelik TTL ile onbelleklenir. Migration, uc plan satirini
**seed eder**:

| plan | rps | burst | monthly_quota | max_page_size | max_concurrency |
|---|---|---|---|---|---|
| free | 2 | 10 | 50 000 | 100 | 2 |
| basic | 10 | 50 | 1 000 000 | 500 | 4 |
| pro | 50 | 200 | 20 000 000 | 1 000 | 8 |

### 4.2 Secret uretimi ve dogrulama

`secrets.token_urlsafe(32)` (256 bit) ile uretilir, YALNIZCA uretildigi
anda bir kez gosterilir, veritabanina **Argon2id** hash'i yazilir
(`argon2-cffi`).

**Parametreler koda sabitlenir ve testte dogrulanir:** `time_cost=2`,
`memory_cost=19456` (19 MiB), `parallelism=1`, `hash_len=32`,
`salt_len=16` (OWASP asgari profili). Daha agir bir profil secilmez ve bu
bilincli bir karardir: secret 256 bit RASTGELE oldugu icin kaba kuvvet
zaten imkansizdir; hash'in amaci yalnizca DB sizintisinda offline
korumadir. Agir profil guvenlik kazanci saglamaz, yalnizca DoS yuzeyini
buyutur (19 MiB x 40 thread = ~760 MiB tavan; 64 MiB'lik varsayilan
profil ayni thread sayisinda 2,5 GB'a cikardi).

Hash islemleri ayri bir semafor ile sinirlanir (varsayilan 8 eszamanli)
ki bellek kullanimi ust sinirli kalsin.

**Sabit zamanli dogrulama tam olarak sudur:** dogrulama yolu, sonuctan
bagimsiz olarak **daima tam iki** Argon2 dogrulamasi kosar — istemcinin
0, 1 veya 2 aktif secret'i olmasindan bagimsiz; eksikler ayni
parametrelerle uretilmis SABIT bir sahte hash ile tamamlanir. Iptal ve
son kullanma kontrolu hash kosulduktan SONRA yapilir, erken cikis yoktur.
Bilinmeyen istemci, yanlis secret ve iptal edilmis secret durumlarinin
ucu de ayni kod (401), ayni govde ve ayni basliklarla yanitlanir;
`error_description` bu uc durumda birebir ayni metindir.

Yalnizca sahte hash yeterli degildir: parametreler sabitlenmezse sahte
hash farkli maliyette kosar ve zamanlama sizintisi geri gelir. Bu yuzden
parametreler burada sabit ve testlidir.

### 4.3 Token uc noktasi

`POST /oauth/token`, `grant_type=client_credentials`.

**Kimlik yalnizca HTTP Basic ile kabul edilir** (`client_secret_basic`).
Govdede `client_secret` gelirse 400 `invalid_request` doner ve bu
OpenAPI'de belgelenir. Basic basligi cozulurken RFC 6749 §2.3.1
uygulanir: base64 cozuldukten sonra `client_id` ve `client_secret`
`application/x-www-form-urlencoded` kuralina gore percent-decode edilir,
ilk iki nokta ust ustesinden bolunur. Ayristirilamayan baslik 400
`invalid_request`'tir.

**`scope` istek parametresi desteklenir** (RFC 6749 §4.4.2): verilirse
istemciye tanimli scope'larin alt kumesi olmalidir, degilse 400
`invalid_scope`. Verilmezse tum tanimli scope'lar verilir. Yanittaki
`scope` alani daima VERILEN scope'lari listeler. Bu destek zorunludur:
`/docs`'taki Swagger `clientCredentials` akisi bu parametreyi gonderir.

Token verilmeden once **DB'den** dogrulanir (Redis'ten bagimsiz):
`api_clients.is_active` ve secilen secret icin
`revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())`.

Yanit: `access_token`, `token_type: Bearer`, `expires_in` (varsayilan
900, `YFAPI_TOKEN_TTL_SECONDS` ile yapilandirilir), `scope`.
Yanit `Cache-Control: no-store` ve `Pragma: no-cache` tasir (RFC 6749
§5.1). Sozlesmede istemcilere `exp`'ten en az 60 saniye once yenilemeleri
soylenir.

**Hata govdesi bu uc noktada RFC 9457 DEGILDIR** — §5.7.

### 4.4 JWT

HS256, baslikta `kid`. Tek servis hem imzalayip hem dogruladigi icin
asimetrik anahtarin bugun karsiligi yok; `kid` anahtar rotasyonunu ve
ileride RS256'ya gecisi eski token'lari kirmadan mumkun kilar.

Claim'ler: `iss`, `aud`, `sub` (client_id), `iat`, `exp`, `jti`,
`scope`, `sid` (kullanilan `api_client_secrets.id`), `epc`
(`api_clients.auth_epoch`).

**`plan` claim'i YOKTUR.** Plan bir yetki degil bir limit parametresidir
ve tek dogruluk kaynagi DB'dir; claim'e konsaydi bir plan dususu token
omru boyunca etkisiz kalir, ayrica "claim mi DB mi baglayici" sorusu
belirsiz kalirdi. Limitler daima 60 sn TTL'li plan onbelleginden okunur.

**Dogrulama kurallari baglayicidir:** `algorithms=["HS256"]` sabit
listesi; `options={"require": ["exp","iat","iss","aud","sub","jti"]}`;
`audience=YFAPI_JWT_AUDIENCE`; `issuer=YFAPI_JWT_ISSUER`; `leeway=30`
saniye. Claim'leri BASMAK ile DOGRULAMAK farkli seylerdir: PyJWT'de
`audience=`/`issuer=` verilmezse `aud` ve `iss` sessizce dogrulanmaz ve
staging anahtari prod'da gecerli olur.

`alg` listeye uymuyorsa (ozellikle `none`) token okunmadan reddedilir.
RS256'ya geciste HS256 ile RS256 **ayni anda kabul edilmez**; gecis iki
farkli `kid` ve iki ayri dogrulama daliyla yapilir — aksi halde klasik
alg-confusion dogrudan token forge'a doner.

`kid` istemci kontrolundedir: yalnizca surec ici sabit bir sozlukte
anahtar aramak icin kullanilir, sozlukte yoksa 401; asla dosya yolu veya
sorgu bileseni olarak kullanilmaz. Imza anahtari en az 32 bayt
rastgeledir ve baslangicta uzunlugu dogrulanir.

`jti` **yalnizca denetim izi** icindir: token verme olayi `jti` ile
loglanir, istek loglari da `jti` tasir. Replay korumasi hedeflenmez —
salt-okuma bir API'de anlami yoktur. Tek bir token'i oldurmek gerekirse
`revoked_jti:<jti>` anahtari (TTL = kalan `exp`) §4.5'teki Redis
okumasina eklenir.

### 4.5 Iptal: epoch mekanizmasi

Dogrulama DB'ye gitmez; iptalin bedeli budur. Yalnizca "istemciyi
pasiflestir" bayragi bu bedeli kapatmaya YETMEZ — sizmis bir secret'in
iptali, scope daraltmasi ve plan dususu de aninda etkili olmalidir.

`api_clients.auth_epoch` sayaci **su islemlerin her birinde artar**:
secret iptali/rotasyonu, scope ekleme/cikarma, plan degisikligi,
pasiflestirme. Artisla birlikte Redis'e `client_epoch:<client_id>`
(TTL 15 dk) yazilir.

Bearer bagimliligi zaten Redis'e ugradigindan, **tek bir `MGET`** ile
`client_disabled:<id>`, `client_epoch:<id>` ve `revoked_secret:<sid>`
okunur. Token'in `epc` degeri Redis'teki epoch'tan kucukse veya `sid`
iptal listesindeyse 401 `invalid_token` doner. Ek DB okumasi yoktur.

Reddedilen oneri: Redis kesintisinde token TTL'ini 15 dk yerine 5 dk'ya
dusurmek. Kesintiyi guvenilir tespit etmek ek bir durum makinesi
gerektirir ve kazanci marjinaldir; TTL sabit kalir.

### 4.6 Scope'lar ve dataset ailesi

Veri ailesi basina, hepsi salt okuma:

`reference:read`, `bars:read`, `fundamentals:read`, `holders:read`,
`news:read`, `discovery:read`, `domains:read`.

**Registry'de bugun "aile" diye bir alan YOKTUR.** `Registrable`
protokolu (`datasets/registry.py:22-27`) yalnizca `name` ve `depends_on`
tasir; `Dataset` (`datasets/base.py:239-244`) `name`, `depends_on`,
`produces`, `date_range` tasir. Dolayisiyla "scope registry'den
turetilir" ancak su degisiklikle dogru olur:

> `Dataset` ve `DomainDataset`/`GlobalDataset` sozlesmesine **yeni
> zorunlu bir `family` alani** eklenir; degeri yukaridaki KAPALI kumeden
> biridir. `Registry.register` bunu dogrular ve eksik/bilinmeyen aileyi
> **kayit aninda** (yani uygulama acilirken, istek aninda degil)
> `ValueError` ile reddeder. Yeni bir dataset eklemek scope tarafinda is
> cikarmaz; yeni bir AILE eklemek ise hem ENUM icin Alembic revizyonu
> hem bu kumeye ekleme gerektirir — bu bilincli bir surtunmedir.

`endpoint_family` (§4.1, §6.4) ayni kapali kumedir, arti `oauth` ve
`meta`. `/health*` olculmez.

### 4.7 Token uc noktasinin limiti

`/oauth/token`'daki `client_id` **henuz dogrulanmamis, saldirgan
kontrolundeki bir dizedir**. Tek anahtar olarak kullanilamaz: saldirgan
kurbanin client_id'sini yanlis secret'la spam'leyip kurbanin kovasini
doldurabilir (hedefli kesinti), ve rastgele client_id'lerle sinirsiz
Redis anahtari uretebilir.

Uc anahtarla islenir, hepsi zorunlu TTL ile ve `client_id`'nin
SHA-256'sinin ilk 16 bayti kullanilarak (sabit uzunluk):

1. `tok:ip:<ip>` — HER istek, TTL 60 sn. Birincil koruma budur.
2. `tok:fail:<h(client_id)>` — yalnizca BASARISIZ dogrulamalar; ust uste
   N hatada o client_id icin 15 dk yavaslatma (bloklama degil).
3. `tok:ok:<h(client_id)>` — yalnizca basarili token vermeler.

### 4.8 CLI

`yfin api client create | list | rotate | enable | disable`.

`disable` ve `rotate --revoke` **iki adimlidir ve Redis yazimi
zorunludur**: DB commit'inden sonra `client_epoch`/`client_disabled`
yazilamazsa komut sifir-disi cikis kodu ve acik uyariyla biter
("istemci DB'de pasiflestirildi ancak yayilmadi; mevcut token'lar en
fazla 15 dk gecerli kalabilir"). Redis'e erisimi olmayan bir ortamda CLI
bunu bastan tespit eder ve islemi reddeder. Aksi halde operator
kapattigini saniyor olurdu.

`enable` `is_active`'i geri acar, `auth_epoch`'u artirir ve
`client_disabled` anahtarini siler.

Portal (alt sistem 2) geldiginde CLI'yi degil ayni `api/storage`
katmanini cagirir.

## 5. Okuma yuzeyi

### 5.1 Cekirdek uc noktalar

| Uc nokta | Scope | Kaynak | Siralama anahtari (imlec) |
|---|---|---|---|
| `GET /v1/symbols` | `reference:read` | `symbols` | `symbol` artan |
| `GET /v1/symbols/{symbol}` | `reference:read` | `symbols` + `ticker_info` | — (tekil) |
| `GET /v1/symbols/{symbol}/bars` | `bars:read` | §5.2 | §5.2 |
| `GET /v1/symbols/{symbol}/actions` | `bars:read` | `v_actions` | `(action_date, action_type)` azalan |
| `GET /v1/symbols/{symbol}/financials` | `fundamentals:read` | `financial_periods` + `financial_facts` | `(period_end, item_key)` azalan |

`/v1/symbols/{symbol}` iki kaynagi okur ama tek scope ister
(`reference:read`): `ticker_info` bir kunyedir, ayri bir ticari deger
tasimaz.

**`GET /v1/symbols` parametreleri:** `exchange`, `quote_type`,
`active` (API adi) -> `symbols.is_active` (DB kolonu), varsayilan
`true`. Varsayilanin `true` olmasi urun kararidir: `is_active=false`
satirlar kesif tarafindan bulunmus ama elle aktiflestirilmemis
sembollerdir ve veri hatti onlari cekmez — yani neredeyse bos kayitlardir.

`q`: **yalnizca `symbol` kolonunda onek aramasi**. Giriste buyuk harfe
cevrilir, `symbol LIKE :q || '%'` olarak kurulur, `%` ve `_` literal
olarak kacirilir, uzunluk 2-32 karakter (disinda 422). Ad uzerinde arama
YOKTUR: `short_name`/`long_name` uzerinde indeks yok, onek aramasi tam
tablo taramasi olurdu.

**`financials` parametreleri** DB ENUM'larindan birebir alinir:
`statement` -> `StatementKind`, `freq` -> `StatementFreq`
(`models/financials.py`). Ikisi de ZORUNLUDUR — verilmezse 422; cunku
`financial_facts` PK'si `(symbol, statement, freq, period_end,
item_key)` ve ikisi verilmeden sorgu indeksin onekini kullanamaz.
Parametre adi bilerek `period` degil `freq`'tir: DB kolonuyla ayni ad.

### 5.2 `bars`: interval, tablo ve seans

Kabul edilen interval'ler ve gittikleri tablo:

| interval | tablo | `session` gecerli mi | imlec anahtari |
|---|---|---|---|
| `1m` `5m` `15m` `60m` | `price_bars` (hypertable) | evet | `ts_utc` |
| `1d` | `price_history` | hayir | `session_date` |
| `1wk` `1mo` | `periodic_bars` | hayir | `ts_utc` |

**`bars_table_for` bugun `1d`'yi BILMIYOR.** `models/bars.py:61-68`:

```python
return "price_bars" if interval in INTRADAY_INTERVALS else "periodic_bars"
```

`BAR_INTERVALS` = `("1m","5m","15m","60m","1wk","1mo")` — `1d` bu kumede
yok, cunku gunluk seri ayri bir tabloya (`price_history`) ve ayri bir
semaya sahiptir. Yani `bars_table_for("1d")` bugun **sessizce
`"periodic_bars"` dondurur**: yanlis tablo, hata yok.

Karar: **`bars_table_for` genisletilir**, API kendi eslemesini yazmaz.

1. `1d` -> `price_history` dali eklenir.
2. Bilinmeyen interval icin `ValueError` firlatilir — bugunku sessiz
   `periodic_bars` donusu gizli bir hatadir ve yazma yolunda da ayni
   riski tasir. Yazma yolu bugun `1d`'yi bu fonksiyona hic sokmadigi
   icin mevcut davranis degismez.
3. Testler `1d` ve bilinmeyen interval icin genisletilir.

**`session` yalnizca intraday interval'lerde anlamlidir.**
`v_price_bars_regular` (`models/views.py:46-52`) `FROM price_bars WHERE
is_extended = false` der; `is_extended` kolonu yalnizca `PriceBar`'da
vardir — `PeriodicBar`'da ve `price_history`'de yoktur, cunku kaynak
yorumun dedigi gibi "seans disi kavrami gun ustu barda ANLAMSIZDIR".

- Intraday: `session` varsayilani `regular`, okuma
  `v_price_bars_regular` uzerinden gider. `session=all` dogrudan
  `price_bars`'a gider.
- `1d`/`1wk`/`1mo`: `session` ACIKCA verilirse 422; verilmezse yok
  sayilir.

Varsayilanin `regular` olmasi kolaylik degil kaza onlemedir: `is_extended`
filtresini unutmak hesaplanan her gostergeyi sessizce bozar.

**Yanit semasi tablo basina farkli alan kumesi tasir** ve bu semada
belgelenir: `price_history` `session_date` + `adj_close` tasir,
`bar_interval`/`local_date`/`is_extended` tasimaz; `price_bars`
`bar_interval`, `local_date`, `is_extended` tasir; `periodic_bars`
`is_extended` tasimaz. Bulunmayan alanlar yaniti kirpmaz, `null` doner.

### 5.3 Zaman alanlari: `session_date` ile `local_date` AYNI SEY DEGILDIR

Bu ayrim yanlis yazilirsa API sessizce yanlis veri doner:

- `price_history.session_date` — borsanin **SEANS gunu**.
- `price_bars` / `periodic_bars` . `local_date` — barin **yerel TAKVIM
  gunu**, seans gunu degil. Kaynak (`models/bars.py:103-107`) adin bilerek
  `session_date` OLMADIGINI yaziyor ve ornek veriyor: GC=F'nin 18:10
  bari, seansi ertesi gune aittir.

API ikisini **farkli adlarla ve farkli anlamda** doner, hicbir yerde tek
alanda birlestirmez. `ts_utc` her ucunde vardir ve RFC 3339 UTC'dir.

**`from`/`to` semantigi:** yari acik aralik `[from, to)` — `from`
kapsayici, `to` dislayici. Ikisi de RFC 3339; yalnizca tarih verilirse
UTC gun basi olarak yorumlanir. Filtre **daima `ts_utc`** uzerinde
kurulur (yerel tarih kolonlari uzerinde degil). `from > to` -> 422.

### 5.4 Genel dataset yuzeyi

- `GET /v1/datasets` — katalog: ad, `family`, kapsam
  (symbol/market/domain — hangi registry ornegine kayitli oldugundan
  turer), gereken scope, kabul edilen parametreler. **Varsayilan olarak
  cagiranin scope'larina filtrelenir**; `?all=true` ile tam katalog
  istenebilir (kasitli, belgelenmis karar).
- `GET /v1/datasets/{ad}` — veri.

Bunun calismasi icin registry'nin API'ye sunmasi gereken **asgari
sozlesme** (§4.6'daki `family` ile birlikte eklenir):

| alan | anlam |
|---|---|
| `family` | scope ve `endpoint_family` kaynagi (kapali kume) |
| `api_sort_key` | keyset sayfalama icin kolon demeti + yon |
| `api_filters` | izin verilen filtre adlari ve tipleri |

Bu ucu tasimayan bir dataset genel yuzeyde **gorunmez** (katalogda
listelenmez, `/v1/datasets/{ad}` 404 doner). Boylece yeni bir dataset
API'yi yanlislikla acmaz; acilmasi bilincli bir eklemedir.

#### Uygulamadan cikan sinir: dataset okuma birimi DEGILDIR

61 dataset'in tamami isaretlenmeye calisilinca su ortaya cikti ve
tasarimin kabul edilmis bir sinridir:

- **Bir dataset birden cok tablo yazabilir.** `search` sekiz, `info` uc,
  `screener` bes tablo yazar. Genel yuzey girdi basina TEK tablo servis
  eder, dolayisiyla bu dataset'ler icin "dataset" yanlis birimdir.
  Okunabilir sey **kaynaktir** (tablo ya da kurgulanmis bir gorunum);
  dataset ise bir YAZMA tarafi kavramidir: bir cekis, bir veya daha cok
  tablo.
- **Bir tabloyu birden cok dataset yazabilir.** `institutional_holders` /
  `mutualfund_holders` ve `earnings_estimate` / `revenue_estimate`
  boyledir. `ApiExposure.fixed` bunu kapatir, ama varliginin sebebi tam
  da eksenin dataset olmasidir.
- **Bazi dataset'lerin okunacak tablosu yoktur** (`sustainability`).

1:1 varsayimi basit ailelerde (holders) tuttugu icin tasarim asamasinda
gorunmedi. Bugunku karar: cok tablolu dataset'ler genel yuzeye
**girmez**; onlarin verisi ya elle tasarlanmis bir uc noktadan gelir ya
da hic gelmez. Ekseni dataset'ten kaynaga tasimak ayri bir karardir ve
§10'da is olarak durur.

OpenAPI'de genel yol TEK bir parametre semasiyla temsil edilir
(`dataset` yol parametresi + ortak filtre nesnesi); dataset basina ayri
sema uretilmez — aksi halde §7.2'deki kilit her yeni dataset'te diff
verirdi.

### 5.5 Sorgu maliyeti tavanlari

K1 (senkron thread havuzu) bu bolumu zorunlu kilar. Tavansiz bir
`interval=1m&from=1980-01-01` istegi milyonlarca satirlik hypertable'i
tarar ve bir thread slotunu dakikalarca tutar; anyio varsayilan havuzu 40
slottur, yani **saniyede 1 istek gonderen bir saldirgan plan limitini
hic asmadan tum API'yi durdurabilir** (hiz limiti orani sinirlar,
eszamanliligi degil).

- **Tarih araligi tavani** (asilirsa 422): intraday <= 31 gun,
  `1d` <= 10 yil, `1wk`/`1mo` <= 50 yil. `to` verilmezse `from + tavan`,
  `from` verilmezse `to - tavan` uygulanir.
- **`statement_timeout`**: her istek `SET LOCAL statement_timeout =
  '10s'` ile acilan bir islemde kosar; asimda 504 (`type:
  query_timeout`).
- **Eszamanlilik semaforu**: istemci basina eszamanli istek sayisi
  Redis'te `api_plans.max_concurrency` ile sinirlanir; asimda 429
  (`type: concurrency_limit`).
- **Boyut sinirlari**: URL <= 2 KiB, tek parametre degeri <= 256
  karakter, istek govdesi <= 8 KiB (GET'lerde govde zaten yok).

### 5.6 Ortak yanit sozlesmesi

**Zarf iki bicimlidir:**

- Koleksiyon yanitlari: `{"data": [...], "next_cursor": null|"...",
  "as_of": "..."}`
- Tekil kaynak yanitlari (`/v1/symbols/{symbol}`): `{"data": {...},
  "as_of": "..."}` — `next_cursor` alani YOKTUR.

**Sayfalama keyset tabanlidir.** `limit` etkin varsayilani
`min(100, plan.max_page_size)`; `limit > plan.max_page_size` gelirse
sessiz kirpma degil **422**. `OFFSET` kullanilmaz: derin sayfalarda
dogrusal yavaslar ve eszamanli yazmada satir atlar.

**Imlec bicimi:** base64url ile kodlanmis kucuk bir JSON —
`{"v": 1, "q": "<sorgu parmak izi>", "k": [...anahtar demeti...]}`.

- `k`, §5.1/§5.2'de uc nokta basina TANIMLI siralama anahtaridir. "Son
  satirin birincil anahtari" yeterli bir tanim degildir: `v_actions` bir
  VIEW'dir ve PK'si yoktur, `price_history` ile `price_bars` farkli PK
  sekillerine sahiptir.
- `q`, istegin imlec-disi tum parametrelerinin kanonik gosteriminin
  SHA-256 kisaltmasidir; sunucu yeniden hesaplar, uymazsa **422**
  (`type: invalid_cursor`). Bu, `interval=1d` sorgusundan alinan imlecin
  `interval=1m` sorgusuna verilmesini engeller — aksi halde farkli tablo
  ve farkli anahtar uzerinde ya 500 ya sessizce yanlis/cok pahali bir
  tarama olurdu.
- `v` uyusmazsa 422; siralama anahtari ileride degisirse eski imlecler
  sessizce yanlis veri dondurmez.
- `k` degerleri Pydantic ile tiplenerek ayristirilir (hata 422, asla
  500) ve daima **bagli parametre** olarak sorguya girer.

**Imlec imzalanmaz.** Yetki karari scope ve yol parametresinden gelir,
imlecten degil; veri kiraciya ozel degil, scope'a gore acik piyasa
verisidir. Sahte bir imlec yalnizca ayni sorgunun baska bir noktasina
atlar. HMAC burada gercek bir tehdit kapatmaz; dogrulama butunluk ve
maliyet icindir.

**DECIMAL alanlar JSON'da string doner.** `PriceType` = `Numeric(28,12)`,
`BigNumType` = `Numeric(38,0)`; float'a cevirmek bunu tam da API
sinirinda sessizce geri alirdi.

**Tazelik (`as_of`): "kaynaga karsi en son ne zaman dogrulandi".** Veri
zamani DEGILDIR ve ikisi asla ayni alanda birlestirilmez.

Tek bir kural yoktur, cunku sema tek bir kaynak sunmuyor. Kaynak veri
ailesine gore degisir:

| Aile | `as_of` kaynagi |
|---|---|
| `fundamentals` | `max(financial_periods.fetched_at)` |
| `holders`, `analysis`, `funds` | `max(asof_state.fetched_at)` (sembol + dataset) |
| `discovery` | `discovery_asof_state` |
| `domains` | `domain_asof_state` |
| `bars`, `reference` | **`null`** — asagiya bakin |

Kayit yoksa `as_of` `null` doner ve `X-Data-As-Of` basligi
**gonderilmez**.

**Fiyat tablolari icin `null` bir eksiklik degil, dogru cevaptir.**
`price_history` / `price_bars` / `periodic_bars` hicbir cekim zaman
damgasi tasimaz ve bu olculmus bir karardir (`models/bars.py`):
`price_bars`'a bir `fetched_at` eklemek arsivde ~4 GB'a mal olur ve
`sync_run_items`'in operator icin zaten cevapladigi bir soruyu
cevaplar. Serinin ne kadar guncel oldugunu son barin `ts_utc`'si zaten
soyluyor ve o yanitin icinde.

Reddedilen uc secenek, gerekceleriyle:

1. **Son barin `ts_utc`'sini `as_of` diye sunmak.** Iki farkli soruyu
   karistirir ve boru hatti o sembolu bir aydir cekemediginde YALAN
   soyler: son bar hala tazeymis gibi gorunur. `session_date` /
   `local_date` ayriminda bir kez odenen bedelin aynisi.
2. **Istek basina `sync_run_items ⨝ sync_runs`.** `(symbol, dataset)`
   basina satir sayisi saklama suresiyle sinirsiz buyur (gunluk kosuda
   yilda ~365 satir, `run_id`'ye gore siralamak icin hepsi okunur), ve
   uc noktayi dataset adina esleyen ikinci bir harita gerektirir. §5.5'in
   onlemek icin var oldugu sey: senkron thread havuzunda her istege
   sinirsiz bir sorgu eklemek.
3. **Kosu duzeyinde tazelik** (son basarili kosunun `finished_at`'i,
   aile basina onbellekli). Ucuzdur ama SEMBOL DUZEYINDE YANLISTIR: son
   kosuda cekilemeyen bir sembol, kosunun bitis zamanini taze diye
   raporlar.

**Dogru cozum kaydedilmistir ve bu spec'in kapsami disindadir:** boru
hattinin yazacagi kucuk bir `dataset_freshness(symbol, dataset,
last_success_at)` tablosu. API tarafinda birincil anahtar aramasi
(bugunku `symbol_exists` sorgusundan ucuz), yazma tarafinda kosu basina
bir upsert — `sync_run_items` satirinin yazildigi ayni islemde. Satir
sayisi sembol x dataset ile sinirlidir (~135 bin), saklama suresiyle
buyumez. Bu bir BORU HATTI degisikligidir; bu spec API veri yazmaz
kuralini korur, dolayisiyla ayri bir is olarak ele alinir.

**Onbellek:** `Cache-Control` daima `private` ile baslar — paylasimli
onbellekler (CDN, kurumsal proxy) bu yanitlari saklayamaz; aksi halde
scope'a ve plana gore degisen bir yanit baska bir istemciye servis
edilebilirdi. `Vary: Authorization, Accept-Encoding` gonderilir.
`max-age`: sorgu araligi dun ve oncesinde bitiyorsa 86400, bugune
dokunuyorsa 60. `ETag`, yanitin tam anahtarindan turetilir: rota +
normalize edilmis tum sorgu parametreleri + `cursor` + `as_of` + sema
surumu. `304` yalnizca bu anahtar birebir ayniysa doner ve hiz sayacina
normal istek gibi dahildir.

**Sembol normalizasyonu:** yol parametresi giriste buyuk harfe cevrilir.
Tablolar `COLLATE "C"` tasir, yani `aapl` != `AAPL`.

### 5.7 Hatalar

`/v1` ve `/health` hatalari RFC 9457 `application/problem+json`:

| Kod | Durum |
|---|---|
| 401 | token yok / gecersiz / suresi gecmis / `epc` eski / istemci pasif |
| 403 | scope yetersiz |
| 404 | bilinmeyen sembol veya dataset |
| 422 | parametre, aralik tavani, gecersiz imlec |
| 429 | hiz, kota veya eszamanlilik siniri |
| 503 | bagimli servis (fail-closed yollar) |
| 504 | sorgu zaman asimi |

`429` ve `503` `Retry-After` tasir. `429`'un uc tipi govdedeki `type`
ile ayrilir: `rate_limit_exceeded`, `quota_exceeded`,
`concurrency_limit`. Odeme anlami tasiyan 402 faturalama alt sistemine
birakilir.

**`/oauth/token` bu kuralin TEK ISTISNASIDIR.** RFC 6749 §5.2 geregi
`Content-Type: application/json` ve govde
`{"error": "...", "error_description": "..."}` bicimindedir; kodlar
`invalid_request` (400), `invalid_client` (401),
`unsupported_grant_type` (400), `invalid_scope` (400). OpenAPI'de ayri
bir `OAuthError` semasi tanimlanir ve `problem+json` semasi bu uc
noktaya baglanmaz. Aksi halde standart istemciler (`authlib`,
`requests-oauthlib`, Go `clientcredentials`, Swagger "Authorize")
yanitta `error` alanini bulamaz ve genellikle "unknown error" firlatir
veya sonsuz yeniden dener — `schemathesis` bunu YAKALAMAZ, cunku
sozlesmeye uygundur.

**`WWW-Authenticate` zorunludur** (RFC 6750 §3). Olmadan istemci "token
suresi doldu, yenile" ile "scope yetersiz, yenilemenin faydasi yok"
ayrimini yapamaz ve scope hatasinda sonsuz token yenileme dongusune
girerek §4.7'deki pahali uc noktaya kendi kendine yuk bindirir.

- 401 (token gecersiz): `Bearer realm="yfin-api", error="invalid_token",
  error_description="..."`; token hic yoksa yalnizca
  `Bearer realm="yfin-api"`.
- 403 (scope): `Bearer error="insufficient_scope", scope="bars:read"`.
- `/oauth/token` 401: `Basic realm="yfin-api", charset="UTF-8"`.

**Kontrol sirasi baglayicidir: kimlik -> scope -> varlik.** Scope
yetersizse kaynagin var olup olmadigina BAKILMADAN 403 doner; 404
yalnizca yetkili cagiran icin uretilir. Aksi halde bir istemci 403/404
farkina bakarak hangi sembol veya dataset'lerin var oldugunu sayabilirdi.

**Govde icerigi:** tum islenmemis istisnalar tek bir isleyicide 500
`problem+json`'a donusturulur; govde yalnizca sabit `type`, `title` ve
`request_id` tasir. Istisna metni, SQL, tablo/kolon adi, dosya yolu ve
yigin izi ASLA govdeye girmez — yalnizca sunucu loguna `request_id` ile
yazilir. 422 govdeleri parametre adi ve kural adini tasir, gonderilen
DEGERI yansitmaz.

## 6. Hiz limiti, kota ve olcum

Iki ayri katman: kisa pencerede **hiz** (altyapiyi korur), uzun pencerede
**kota** (urunu sinirlar).

### 6.1 Hiz

Redis'te token bucket, tek bir Lua script'iyle atomik. Oku-hesapla-yaz
uc ayri komut olsaydi eszamanli isteklerde limit sizardi. Token bucket
secildi cunku sabit pencere sinirda iki kati trafige izin verir, kayan
pencere logu istek basina kayit tutar; token bucket kisa burst'lere
bilerek izin verir ve `Retry-After`'i tam hesaplar.

Sayac anahtari `client_id`'dir (token dogrulanmis oldugu icin
guvenilirdir). Limit degerleri plan onbelleginden okunur (§4.4: token'da
`plan` claim'i yoktur).

**Basliklar:** `RateLimit-Limit: <n>`, `RateLimit-Remaining: <n>`,
`RateLimit-Reset: <saniye>` (tam sayi saniye, mutlak zaman damgasi
degil). Kota icin ayri aile: `X-Quota-Limit`, `X-Quota-Remaining`,
`X-Quota-Reset` (ay sonuna kalan saniye).

### 6.2 Kota

Anahtar `quota:{client_id}:{YYYY-MM}`, pencere **UTC**'dir.

Kota, hiz kontrolüyle **ayni Lua script'inde** ve istegin **basinda**
degerlendirilir: script sayaci okur, tavani asiyorsa **artirmadan**
reddeder, asmiyorsa atomik olarak artirir ve gerekirse `EXPIRE`'i ayni
cagrida kurar. `INCR` ile `EXPIRE` asla ayri komut degildir: aralarinda
surec olurse anahtar kalici olur ve musterinin sayaci ertesi ay
sifirlanmaz.

**Ne sayilir:** 2xx ve 4xx istemci hatalari sayilir. 5xx ile bitenler ve
hiz limiti ile reddedilenler sayilmaz — istek sonunda telafi azaltmasi
yapilir. Aksi halde musteri "sizin hatanız yuzunden kotam yandi" demekte
hakli olurdu.

### 6.3 Redis kesintisi: fail-open, ama her yerde degil

**Okuma uc noktalarinin hiz ve kota sayaclari fail-open'dir.** Salt sayac
tutan bir bilesenin urunun tamamini yere indirmesi orantisiz olurdu.

**`/oauth/token` fail-CLOSED'dur.** Redis dustugunde §4.7'deki siki limit
de duserdi; sonuc yalnizca "sayac kaybi" degil, **kimlik dogrulama
katmaninin brute-force korumasinin tamamen kalkmasi** ve §4.2'nin kendi
ifadesiyle "tek basina bir DoS yuzeyi" olan Argon2 dogrulamasinin
sinirsiz tetiklenebilmesidir. Redis erisilemezse surec-ici bellekte
tutulan yedek bir sayac devreye girer (IP basina dakikada N deneme); o da
asilirsa 503 + `Retry-After`.

**Iptal/epoch kontrolu fail-open kalir** (aksi halde tum trafik duser);
penceresi token TTL'i kadardir, yani en fazla 15 dakika.

**Kota icin "en kotu hal 15 dakika" DEGILDIR:** o argüman yalnizca iptal
icin gecerlidir. Hiz ve kota fail-open penceresi **kesinti suresi
kadardir**. Kesinti sirasinda sayilamayan istekler `api_usage_daily`'ye
`estimated=true` bayragiyla yazilir ve kotadan geriye donuk dusulmez.

Her fail-open `error` seviyesinde loglanir ve metriklesir.

### 6.4 Olcum

Istek sayaclari gunluk olarak `api_usage_daily`'ye aktarilir: istemci x
gun x `endpoint_family` (§4.6'daki kapali kume + `oauth`, `meta`).
`/health*` olculmez.

"Son kullanim zamani" her istekte `UPDATE` olmaz; Redis'te tutulur ve
dakikada bir `api_clients.last_used_at`'e yazilir.

## 7. Sozlesme, yapilandirma, calistirma

### 7.1 Bagimliliklar

```toml
[project.optional-dependencies]
api = ["fastapi", "uvicorn[standard]", "pyjwt", "argon2-cffi", "redis",
       "python-multipart"]
dev = ["pytest>=8.0", "ruff>=0.5", "mypy>=1.10",
       "httpx", "schemathesis", "fakeredis[lua]"]
```

Veri hatti sunucularina FastAPI kurmanin anlami yok; `yfinance[repair]`
ile ayni desen.

Iki paket uygulama sirasinda eklendi ve ikisi de opsiyonel degil:

- **`python-multipart`** — RFC 6749 token uc noktasinin
  `application/x-www-form-urlencoded` govde kabul etmesini zorunlu kilar
  ve FastAPI'nin `Form()`'u bu paket olmadan govdeyi ayristirmayi
  reddeder.
- **`fakeredis[lua]`** (`lupa`) — hiz ve kota tek bir Lua script'inde
  karara baglandigi icin, `[lua]` olmadan o testler ancak gercek bir
  sunucuya karsi kosabilirdi.

### 7.2 OpenAPI kilidi

`scripts/dump_openapi.py` uygulamadan `openapi.json` uretir; dosya depoda
tutulur, CI ayni script'i `--check` ile kosar ve diff varsa build
kirilir. Bir Pydantic alanini yeniden adlandirmak incelemede GORUNUR bir
diff olur.

`securitySchemes`: `oauth2 / clientCredentials`, `tokenUrl:
/oauth/token`, tum scope listesiyle — `/docs`'taki "Authorize" dugmesi
gercekten calisir (bunun icin §4.3'teki `scope` istek parametresi
desteklenmek zorundadir). Her uc nokta gerektirdigi scope'u FastAPI
`Security` bagimliligi uzerinden bildirir; semadaki scope ile kodda
UYGULANAN scope ayni bildirimden gelir.

### 7.3 Surumleme ve emeklilik

Yol tabanli `/v1`.

**Kirici sayilanlar:** yanit alani kaldirmak veya tipini degistirmek,
zorunlu istek parametresi eklemek, mevcut bir parametrenin kabul
araligini **daraltmak** (ornegin §5.5'teki tarih tavanini sonradan
koymak), varsayilan degeri veya siralama duzenini degistirmek, yeni bir
zorunlu scope talep etmek.

**Kirici sayilmayanlar:** yanit alani eklemek, yeni istege bagli
parametre, yeni uc nokta, yeni `problem+json` `type` degeri, bir enum'a
deger eklemek. Sozlesme, istemcilerin bilinmeyen alanlari ve bilinmeyen
enum degerlerini tolere etmesi gerektigini yazar.

**Emeklilik:** emekliye ayrilan uc nokta/surum yanitlari `Deprecation` ve
`Sunset` basliklarini tasir, OpenAPI'de `deprecated: true` isaretlenir;
`Sunset` en az **6 ay** sonrasidir (ticari bir karardir, alt sistem 3
geldiginde yeniden degerlendirilebilir). Degisiklikler CHANGELOG'da
yayimlanir.

### 7.4 Yapilandirma

API'nin kendi `BaseSettings`'i (`YFAPI_` oneki, `api/core/config.py`).
Mevcut `Settings` (`config.py:68`) `env_prefix` KULLANMAZ; alan adlari
zaten `yf_*`/`db_*` oldugu icin cakisma olmaz — `YFAPI_` oneki API
ayarlarini ayirmak icindir, mevcut bir desenin tekrari degildir.

Ayirma iki gerekceyle: bunlar `settings` tablosunun yonettigi veri hatti
ayarlari degil, ve JWT imza anahtari bir SIRDIR — `yf_proxy_secret_key`
gibi (`config.py:333`, `ENV_ONLY_FIELDS`) ortamda kalir, panelden
duzenlenebilir olmaz.

Asgari ayarlar: `YFAPI_JWT_SIGNING_KEY`, `YFAPI_JWT_KID`,
`YFAPI_JWT_ISSUER`, `YFAPI_JWT_AUDIENCE`, `YFAPI_TOKEN_TTL_SECONDS`,
`YFAPI_REDIS_URL`, `YFAPI_TRUSTED_PROXIES`, `YFAPI_CORS_ORIGINS`,
`YFAPI_DOCS_ENABLED`.

### 7.5 Calistirma, ag ve gozlemlenebilirlik

`docker-compose.yml`'ye `redis` ve `api` servisleri; Redis imaji da
TimescaleDB gibi SABIT surume cakilir.

Compose ayrica tek dugumlu, sabit surumlu bir **Kafka** brokeri tasir
(KRaft). Bugun hicbir kod ona baglanmaz; altyapinin hazir olmasi icin
istendi. Iki dinleyici ile kurulur ve bu zorunludur: Kafka istemciye
yeniden baglanacagi ADVERTISED adresi soyler, dolayisiyla `kafka:9092`
duyuran bir broker host'tan, `localhost:9092` duyuran ise diger
konteynerlerden erisilemez olur -- her iki hata da AYAGA KALKAR ve
saglik kontrolunu GECER, ancak ilk uretim denemesinde ortaya cikar.

**TLS zorunludur.** API yalnizca TLS uzerinden yayinlanir; sonlandirma
reverse proxy'dedir, duz HTTP dinleyicisi ya kapalidir ya yalnizca 308
ile HTTPS'e yonlendirir. §4.3 client secret'i Basic basliginda (base64,
sifresiz) tasidigi icin tek bir `http://` istemcisi kalici sir sizintisi
demektir.

**Guvenlik basliklari:** tum yanitlarda `Strict-Transport-Security:
max-age=31536000; includeSubDomains`, `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer`.

**Proxy guveni.** `YFAPI_TRUSTED_PROXIES` (CIDR listesi) tanimlanir;
`ProxyHeadersMiddleware` yalnizca bu aglardan gelen baglantilarda
`X-Forwarded-For`'u dikkate alir ve **sagdan itibaren guvenilen proxy
sayisi kadar atlayarak** istemci IP'sini secer. Liste bossa proxy
basliklari tamamen yok sayilir. Aksi iki yonlu bozulur: ayar yoksa tum
dunya reverse proxy'nin tek IP'sinde toplanir; korlemesine ilk deger
alinirsa saldirgan basligi uydurup limiti atlar ve kurbanin IP'sini
yazarak onu bloklatir.

**Saglik uc noktalari.** `/health` (liveness) ve `/health/ready`
(DB + Redis) kimlik dogrulamasi istemez, ama limitsiz DEGILDIR:
`/health/ready` sonucu 5 saniye surec ici onbelleklenir ve IP basina
dakikada 60 istekle sinirlidir. Aksi halde her cagrida DB ve Redis'e
dokunan, kimlik dogrulamasiz ve limitsiz bir uc nokta API'nin en ucuz
DoS yuzeyi olurdu. Govde yalnizca `{"status": "ok"|"degraded"}` ve
bilesen basina `ok/fail` tasir; surum, ana bilgisayar adi, baglanti
dizgisi veya istisna metni icermez.

**Loglama.** Istek basina yapilandirilmis log mevcut `structlog`
uzerinden: `request_id`, `client_id`, `jti`, rota **sablonu**, sure,
durum kodu. `Authorization`, `Cookie` basliklari ve `client_secret` /
`access_token` iceren her alan structlog islemcisinde kosulsuz redakte
edilir; **query string loga alinmaz** (bir istemci sirri yanlislikla
query'de gonderirse loga dusmesin diye). CLI `create` / `rotate`
ciktisindaki secret stdout'a yazilir, log dosyasina asla.

**CORS** varsayilan kapali, allowlist ile acilir. `/docs` ve
`/openapi.json` varsayilan olarak aciktir (`YFAPI_DOCS_ENABLED` ile
kapatilabilir) ve `/oauth/token` ile ayni IP limitine tabidir.

## 8. Dogrulama

- **Birim:** Argon2 parametrelerinin sabitligi ve "daima iki dogrulama"
  invaryanti; JWT uretimi/dogrulamasi (suresi gecmis, `kid` bilinmeyen,
  imza bozuk, `alg=none`, yanlis `aud`, yanlis `iss`, leeway siniri);
  scope kontrolu; token bucket ve kota Lua script'i (`fakeredis`);
  imlec kodlama/cozme + parmak izi uyusmazligi; `bars_table_for`'un
  `1d` ve bilinmeyen interval davranisi.
- **Sozlesme:** `schemathesis`, islenmis `openapi.json`'a karsi.
- **Entegrasyon** (`repo` marker'i, gercek PostgreSQL + `fakeredis`):
  token al -> uc noktayi cagir -> scope'suz cagir (403, varlik
  kontrolunden ONCE) -> limiti tasir (429) -> `rotate` ile secret'i
  iptal et -> eski secret'la alinmis token'in aninda reddedildigini gor
  (`epc` / `sid`) -> istemciyi pasiflestir -> 401.
- **Regresyon:** tarih araligi tavani, `statement_timeout`, eszamanlilik
  semaforu; `session` parametresinin `1d`/`1wk` ile 422 vermesi.
- `mypy --strict` `api/` paketini otomatik kapsar (`files = ["src"]`);
  `ruff` ayni kurallarla.

## 9. Uygulama sirasi

0. **CI iskeleti** — `.github/workflows/ci.yml`: ruff + `mypy --strict`
   + pytest + (ilerideki) `dump_openapi.py --check`. Depoda bugun hicbir
   CI yok; K5 bu adim olmadan uygulanamaz.
1. **Yeniden yapilandirma** — paket tasimasi + `storage/contracts.py`
   (protokoller **ve** `apply_write`). Kendi kabul olcutu (§3.1) ile
   biter, API kodu ONCESINDE.
2. **Altyapi** — `api` ekstrasi, Redis servisi, `YFAPI_` ayarlari,
   `app.py` iskeleti, guvenlik basliklari + proxy guveni middleware'i,
   CORS, `request_id`'li structlog ve redaksiyon islemcisi, `/health`
   (onbellekli + IP limitli).
3. **Istemci modeli** — Alembic revizyonu (5 tablo + `api_plans`
   seed'i), Argon2 hash'leme, `api/storage` istemci deposu,
   `yfin api client create | list | rotate | enable | disable`.
4. **Token akisi** — `/oauth/token` (RFC 6749 hata govdesi, `scope`
   parametresi, Basic cozumleme), JWT uretim/dogrulama sertlestirmesi,
   Bearer bagimliligi, scope kontrolu, `epc`/`sid` iptal mekanizmasi ve
   **token uc noktasinin kendi fail-closed limiti** (§4.7, §6.3).
5. **Hiz, kota, semafor** — Lua script (hiz + kota tek cagrida), plan
   onbellegi, `RateLimit-*` / `X-Quota-*` basliklari, 429 tipleri,
   `api_usage_daily` toplu yazimi, `last_used_at` tamponu.
6. **Cekirdek uc noktalar** — symbols, bars (`bars_table_for`
   genisletmesi dahil), actions, financials; ortak zarf, keyset imlec,
   `as_of` + `X-Data-As-Of`, `ETag` / `Cache-Control`, sorgu maliyeti
   tavanlari, `statement_timeout`.
7. **Genel dataset yuzeyi** — `Dataset` sozlesmesine `family` /
   `api_sort_key` / `api_filters` eklenmesi, katalog (scope'a
   filtrelenmis), `/v1/datasets/{ad}`.
8. **Sozlesme kilidi** — `dump_openapi.py`, `openapi.json`, CI diff,
   schemathesis.

## 10. Kayitli takip isleri

Bu spec'in kapsami disinda kalan, ama uygulama sirasinda adiyla
kararlastirilan isler. Kaybolmasinlar diye burada dururlar.

- **`dataset_freshness` tablosu (boru hatti).** Fiyat aileleri icin
  gercek bir "en son ne zaman dogrulandi" degeri uretir; §5.6'daki
  gerekce ve reddedilen alternatifler orada. API tarafinda birincil
  anahtar aramasi, yazma tarafinda kosu basina bir upsert.
- **Compose'daki `api` servisi ve Dockerfile.** Uc noktalar var artik;
  kalan is imaji ve servisi yazmaktir.
- **Kafka icin bir uretici/tuketici.** Broker compose'da ayakta. Amaci
  bu spec yazildiktan sonra netlesti: canli WebSocket akisi tasariminin
  (`2026-09-06-websocket-streaming-design.md`, K5) transactional outbox
  uzerinden OPSIYONEL yayin yolu. Bu API'nin okuma yuzeyi ona baglanmaz.

- **Kalan dataset'lerin isaretlenmesi.** 19 dataset acik. Kalanlar uc
  gruba ayrilir ve yalnizca ucuncusu mekanik bir istir: (a) elle
  tasarlanmis uc noktalarin zaten servis ettikleri -- ikinci bir yol
  ACILMAMALIDIR, cunku o yol interval yonlendirmesi, seans filtresi ve
  aralik tavanlari olmadan gelir; (b) cok tablolu olanlar -- §5.4'teki
  eksen sorunu; (c) o an baska bir oturumun duzenledigi dosyalardakiler.

- **Okuma ekseninin dataset'ten kaynaga tasinmasi.** §5.4'teki sinirin
  gercek cozumu. Bugun gerekmiyor: cok tablolu dataset'lerin verisine
  talep olmadan yapilirsa YAGNI ihlalidir.
