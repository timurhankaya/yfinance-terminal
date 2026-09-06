# MySQL 8 → PostgreSQL 18 + TimescaleDB — Tasarım Dokümanı

**Tarih:** 2026-09-04
**Durum:** onaylandı, uygulanmayı bekliyor
**Sürüm:** v2 — üç bağımsız incelemeden sonra düzeltildi (bkz. Ek B)
**Kapsam:** Veri hattının depolama motorunun tamamen değiştirilmesi.
`src/`, `tests/`, `migrations/`, `scripts/`, `pyproject.toml` içinde MySQL'e
ait sıfır referans kalır.
**Kısaltma:** Bu dokümanın bölümleri metinde `§X.Y`, ASCII docstring'lerde
`PG§X.Y` olarak anılır.

**Önceki dokümanlar (tarihsel kayıt):**
`2026-09-04-yfinance-mysql-etl-design.md` (S),
`2026-09-04-yfinance-financials-market-design.md` (F),
`2026-09-04-yfinance-analysis-holdings-design.md` (AH),
`2026-09-04-proxy-pool-and-yfinance-advanced-design.md` (P),
`2026-09-04-price-history-service-design.md` (PB),
`2026-09-04-sector-industry-design.md` (SI).

Bu doküman yukarıdakilerin **motora ait** kararlarını geçersiz kılar
(tip seçimi, collation, partition, kilit, upsert mekaniği). **Veriye ait**
kararları (ölçülen max uzunluklar, anahtar semantiği, as-of kapıları,
monotonik kolonlar, kapsam kuralları) aynen KORUR. Eski dokümanlar
silinmez; içerdikleri ölçümler hâlâ geçerli kanıttır. Metinde yalnızca
`PB` kısaltmasına atıf yapılır; diğerleri bu listede kimlik için durur.

---

## 0. Önkoşullar ve doğrulanmış gerçekler

| Gerçek | Ölçüm |
|---|---|
| Kullanılacak imaj | `timescale/timescaledb:2.29.2-pg18` (§1) |
| PostgreSQL sürümü | **18.6** |
| TimescaleDB sürümü | **2.29.2** (ölçüm tarihindeki `latest-pg18`) |
| Yerel 5432 portu boş | `lsof -iTCP:5432 -sTCP:LISTEN` |
| `Base.metadata` tablo sayısı | **72** |
| `__tablename__` tanımlayan model dosyası | **15** (+ `snapshots.py` `Table()` ile 5 tablo) |
| `MYSQL_TABLE_ARGS` kullanan dosya | **17** |
| `migrations/versions` revizyon sayısı | **12** |
| Native ENUM tipi | **16** (§2.9) |
| Unsigned tamsayı kolonu | **40** (§2.6) |
| `server_default` taşıyan `Boolean` kolonu | **20** (§2.10) |
| `MySQLRowWriter` geçen dosya | **17** (§4) |
| `datetime.now(UTC).replace(tzinfo=None)` | **13 dosya, 18 satır** (§2.3) |
| Diğer `.replace(tzinfo=None)` | **26 satır** (§2.3) |
| `tzinfo is None` iddia eden test dosyası | **4** (§2.3) |
| `TableWrite(...)` çağrısı | **72** (§4.1.1) |
| MySQL metni geçen dosya | **84** (§13) |
| `TableWrite.key_columns` ↔ PK/UNIQUE | statik çözülebilen çağrıların tamamı eşleşiyor |

> **BU SAYILAR BİR ANLIK GÖRÜNTÜDÜR, SÖZLEŞME DEĞİL.** Kod tabanı bu
> tasarım yazılırken de gelişmeye devam etti: Search/Lookup/Screener (SQ)
> alt sistemi eklendiğinde tablo sayısı 62'den 72'ye, ENUM 14'ten 16'ya,
> unsigned kolon 35'ten 40'a çıktı. SQ **yeni bir MySQL bağlanma TÜRÜ
> getirmedi** — aynı `MYSQL_TABLE_ARGS` / `dialects.mysql` / `SMALLINT`
> desenlerinin daha fazla örneği. Bu yüzden tasarımın yapısı değişmedi.
>
> Uygulama sırasında **hiçbir adım bu tablodaki sayıya güvenmez**: her
> görev kendi otorite listesini canlı bir `grep`/`Base.metadata` sorgusuyla
> türetir (§14). Buradaki sayılar yalnızca büyüklük duygusu verir ve
> beklenmedik bir sapmayı fark etmeye yarar.

Son satır `ON CONFLICT`'in ön koşuludur ve §9.2'de çalışma zamanı
invaryant testiyle kalıcılaştırılır.

**Veri taşınmıyor.** Proje development aşamasındadır; mevcut MySQL verisi
terk edilir, şema sıfırdan kurulur. Bu dokümanda hiçbir yerde veri
göçü, çift yazma veya MySQL'e geri dönüş yolu tanımlanmaz — bilinçli
olarak.

**Sürüm sabitlenir.** `latest-pg18` değişken bir tag'dir; bu oturumda
2.26.3 beklenirken 2.29.2 geldi. §15/7'deki "sıfırdan çalışıyor" ölçütünün
zaman içinde farklı bir motora karşı koşmaması için compose sabit tag
kullanır.

---

## 1. Altyapı: Docker

Proje kökünde `docker-compose.yml` (şu an böyle bir dosya YOK).

```yaml
services:
  timescaledb:
    image: timescale/timescaledb:2.29.2-pg18
    container_name: yfin-timescaledb
    environment:
      POSTGRES_USER: ${DB_USER:-yfin}
      POSTGRES_PASSWORD: ${DB_PASSWORD:?DB_PASSWORD gerekli}
      POSTGRES_DB: ${DB_NAME:-yfinance}
      TZ: UTC
      PGTZ: UTC
    command:
      - postgres
      - -c
      - max_connections=200
      - -c
      - timescaledb.max_background_workers=8
    ports: ["${DB_PORT:-5432}:5432"]
    # MOUNT `/var/lib/postgresql` -- `/data` ALT DIZINI DEGIL.
    # PostgreSQL 18 imaji konvansiyonu degistirdi; eski yol konteyneri
    # Exited(1) yapar ("There appears to be PostgreSQL data in
    # /var/lib/postgresql/data (unused mount/volume)"). Uygulama
    # sirasinda olculdu.
    volumes: ["yfin-pgdata:/var/lib/postgresql"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${DB_USER:-yfin} -d ${DB_NAME:-yfinance}"]
      interval: 5s
      timeout: 5s
      retries: 20
volumes:
  yfin-pgdata:
```

**`max_connections=200` gerekçesi.** `db.py` havuz formülü process başına
`pool_size + max_overflow = 2 × max(5, workers+4)` bağlantı tavanı koyar;
varsayılan `workers=4` ile shard başına 16, `yf_max_shards=4` ile toplam
`4×16 + 2 = 66`. 200 rahat bir paydır.

**Port env üzerinden.** `${DB_PORT:-5432}` compose ve `config.py` tarafından
aynı `.env` anahtarından okunur; ileride bir çakışma olursa tek yerden
değiştirilir.

**Parola zorunlu.** `${DB_PASSWORD:?...}` boş parolayla ayağa kalkmayı
engeller. MySQL kurulumunda `db_user=root, db_password=""` idi; bu taşınmaz.

**`timescaledb-ha` imajı KULLANILMAZ.** İçindeki ek eklentiler
(`timescaledb_toolkit`, `postgis`) bu hatta gerekmiyor.

---

## 2. Tip katmanı

### 2.1 Tablo argümanları

`MYSQL_TABLE_ARGS` **tamamen kaldırılır** — 62 tablonun tamamında, 15
dosyada geçiyor. Tek elemanı `MYSQL_TABLE_ARGS` olan tablolarda
`__table_args__` satırı bütünüyle silinir.

### 2.2 Tip haritası

| MySQL | PostgreSQL 18 | Not |
|---|---|---|
| `DATETIME(fsp=6)` | `postgresql.TIMESTAMP(timezone=True, precision=6)` | §2.3 |
| `LONGTEXT` (`raw_json`) | `TEXT` | JSONB **değil**, §2.4 |
| `MEDIUMTEXT` (`report_title`) | `TEXT` | |
| `VARCHAR(n)` + charset/collation | `VARCHAR(n) COLLATE "C"` | §2.5 |
| `String(n)` (`kinds.py`) | `String(n, collation="C")` | §2.5, ATLANMAMALI |
| `BIGINT UNSIGNED` | `BIGINT` + `CHECK (c >= 0)` | §2.6 |
| `TINYINT UNSIGNED` (`holding_rank`) | `SMALLINT` + `CHECK 0..255` | §2.6 |
| `SMALLINT UNSIGNED` (`proxies.port`) | `INTEGER` + `CHECK 1..65535` | §2.6 |
| `VARBINARY(512)` (`password_enc`) | `BYTEA` | §2.7 |
| `BIGINT UNSIGNED AUTO_INCREMENT` | `BIGINT GENERATED BY DEFAULT AS IDENTITY` | §2.8 |
| native `ENUM` | PostgreSQL native `ENUM` tipi | §2.9 |
| `CURRENT_TIMESTAMP ON UPDATE ...` | **karşılığı yok** | §2.11 |
| `Numeric(p,s)`, `Date`, `Integer`, `SMALLINT` | değişmez | |

### 2.3 Zaman damgaları: `timestamptz(6)`

```python
from sqlalchemy.dialects.postgresql import TIMESTAMP

def TsType() -> TIMESTAMP:
    return TIMESTAMP(timezone=True, precision=6)
```

**Generic `sqlalchemy.TIMESTAMP` `precision` KABUL ETMEZ**
(`TypeError: unexpected keyword argument 'precision'`). Dialect tipi
zorunludur — mevcut `dialects.mysql.DATETIME(fsp=6)` deseninin birebir
muadili. Ölçüldü: `postgresql.TIMESTAMP(timezone=True, precision=6)` →
`TIMESTAMP(6) WITH TIME ZONE`.

**DEĞİŞMEYEN gerekçe:** hassasiyet 6 hane kalır. `ticker_info_history` ve
`ticker_fast_info_history` PK'sı `(symbol, fetched_at)`; saniye
hassasiyeti aynı saniyede iki snapshot'ta PK çakışması üretirdi. PostgreSQL
de kesirli kısmı **yuvarlar** (kesmez): `timestamptz(0)` ile `.9` → +1 sn.
İnvaryant testi korunur (§9.4).

**DEĞİŞEN:** kolon artık tz taşır.

- `normalize.py:to_datetime_utc` / `epoch_to_datetime` **`.replace(tzinfo=None)`
  yapmayı bırakır**; UTC-aware `datetime` döndürür.
- `datetime.now(UTC).replace(tzinfo=None)` deseni **12 dosyada, 17 satırda**:
  `cli.py`, `cli_bars.py`, `datasets/bars.py`, `domain_runner.py`,
  `market_runner.py`, `proxy/repository.py`, `rescale.py`, `runner.py`,
  `shard.py` (src) ve `tests/live/test_live_analysis.py`,
  `tests/live/test_live_bars.py`, `tests/repo/test_proxy_repo.py`.
  Hepsi `datetime.now(UTC)` olur.
- Ayrıca **24 satır** daha `.replace(tzinfo=None)` içeriyor (30 dosyada
  toplam). Bunlar tek tek incelenir: kaynağı tz-aware bir damgayı naive'e
  indirenler kaldırılır, `date`/`datetime` kurgusu yapanlar korunur.
  Otorite listesi: `grep -rn 'replace(tzinfo=None)' src tests scripts`.
- `cli.py:_parse_date` UTC-aware üretir.
- `assert ...tzinfo is None` iddiaları **4 test dosyasında**:
  `test_bars_normalize.py`, `test_financials.py`, `test_market.py`,
  `test_normalize.py`. Hepsi `tzinfo is UTC`'ye döner.

**Atlanan bir dosya SESSİZ kalır:** psycopg3 naive datetime'ı bağlantı
TZ'sine (UTC, §5.4) göre yorumlar, yani sonuç doğru çıkar ama tip
tutarsızlığı kalıcılaşır. Bu yüzden tarama grep çıktısı üzerinden
yapılır, hafızadan değil.

**`local_date` ve `session_date` `DATE` KALIR.** Bunlar mutlak zaman değil,
borsanın yerel takvim günüdür (PB§5). `timestamptz`e çevirmek, PB§5'te
açıkça reddedilen kavram karışıklığını geri getirirdi.

### 2.4 `raw_json`: `TEXT`, JSONB değil

`RawJsonType()` → `Text()`.

MySQL'de `LONGTEXT` seçilmesinin üç gerekçesi (anahtar sırası değişir,
NaN reddedilir, float DOUBLE'a düşer) **PostgreSQL `jsonb` için aynen
geçerlidir**. `json` tipi metni korur ama yine sözdizimi doğrular ve NaN'ı
reddeder. Byte-for-byte sadakat `content_hash`'in ön koşuludur.

### 2.5 Collation: her yerde `"C"`, kodda açık normalizasyon

| Fabrika / kaynak | MySQL | PostgreSQL |
|---|---|---|
| `SymbolType()` | `ascii_bin` | `VARCHAR(32) COLLATE "C"` |
| `NewsIdType()` | `ascii_bin` | `VARCHAR(36) COLLATE "C"` |
| `HashType()` | `ascii_general_ci` | `VARCHAR(64) COLLATE "C"` |
| `ShortHashType()` | `ascii_bin` | `VARCHAR(16) COLLATE "C"` |
| `BarIntervalType()` | `ascii_bin` | `VARCHAR(4) COLLATE "C"` |
| `RegionType()` | `ascii_bin` | `VARCHAR(16) COLLATE "C"` |
| `AsciiKeyType(n)` | `ascii_bin` | `VARCHAR(n) COLLATE "C"` |
| `ProxyLabelType()` | `ascii_bin` | `VARCHAR(64) COLLATE "C"` |
| `PersonNameType()` | `utf8mb4_0900_as_cs` | `VARCHAR(255) COLLATE "C"` |
| `KeyTextType(n)` | `utf8mb4_0900_as_cs` | `VARCHAR(n) COLLATE "C"` |
| `HostType()` | `ascii_general_ci` | `VARCHAR(255) COLLATE "C"` + §2.5.2 |
| **`kinds.py` `str16/32/64/128/255`** | tablo varsayılanı `utf8mb4_0900_ai_ci` | **`String(n, collation="C")`** |
| **model dosyalarında doğrudan `String(n)`** | tablo varsayılanı `utf8mb4_0900_ai_ci` | **`String(n, collation="C")`** |

**Son İKİ satır atlanamaz — üç kaynak vardır, biri değil.**

1. `models/base.py` fabrikaları (yukarıdaki 11 satır)
2. `models/kinds.py` `strN` kind'ları — `ticker_info`, `ticker_fast_info`,
   `history_metadata` ve türevlerinin büyük kısmı
3. **Model dosyalarında doğrudan yazılmış `String(n)` çağrıları** — ölçüldü:
   **89 çağrı, 12 dosyada** (`discovery.py` 26, `market.py` 19,
   `domains.py` 14, `symbols.py` 9, `news.py` 5, `analysis.py`/`funds.py`/
   `holders.py`/`sync.py` 3'er, `financials.py` 2, `bars.py`/`officers.py`
   1'er)

Üçüncü kaynak bu tasarımın ilk sürümünde **gözden kaçmıştı** ve
uygulama sırasında §9.4'teki invaryant testi tarafından yakalandı: 101
kolon `"C"` olmadan kalıyordu. Testin varlık sebebi tam olarak budur —
"politikayı bir yardımcıda tanımlamak onu korumaz".

MySQL'de üçü de tablo varsayılanını alıyordu; PostgreSQL'de kolon
collation'ı verilmezse **veritabanı varsayılanı** (imajda `en_US.utf8`)
uygulanır, yani ne `"C"` ne de eski davranış.

**Kabul edilen bedel:** salt görüntüleme alanları da (`domains.name`,
`search_quotes.long_name`) `"C"` olur, yani `ORDER BY` byte sıralıdır ve
aksanlı metinde dile göre doğru sıralamaz. Alternatif — anahtar olmayan
kolonları veritabanı varsayılanına bırakmak — invaryantı yok eder ve
tam da bu bölümün önlediği sessiz ayrışmayı geri getirirdi. Sıralama
gerekirse sorgu tarafında `COLLATE "en_US.utf8"` ile istenir.

**`"C"` collation seçiminin kanıtı (ölçüldü):** `"C"` collation'lı kolonda
`LIKE 'abc%'` `text_pattern_ops` OLMADAN indeks kullanıyor
(`Index Cond: (s >= 'abc' AND s < 'abd')`); aynı tabloda `en_US.utf8`
kolonda aynı sorgu `Seq Scan`'e düşüyor.

MySQL'in `ai_ci` varsayılanı kodda **iki** yerde gerçek semantik taşıyor.
Üçüncü bir aday incelendi ve **bağımlılık olmadığı** görüldü (§2.5.3).

#### 2.5.1 `cli.py:_filtered_symbols` — borsa / tip filtresi

Mevcut yorum: *"`func.upper` KULLANILMAZ: kolonlar `utf8mb4_0900_ai_ci`'dir
(zaten büyük/küçük harf duyarsız) ve fonksiyon sarmalamak `exchange`
indeksini kullanılamaz hale getirirdi."* PostgreSQL'de bu gerekçe çöker.

Çözüm:
- `--exchange` / `--quote-type` argümanları CLI'da `.strip().upper()`.
- `symbols.exchange` ve `symbols.quote_type` **yazılırken de** `.upper()`
  (`datasets/symbols.py:58-59`). İki taraf da normal biçimde olur ve
  `ix_symbols_exchange` (`models/symbols.py:16`, mevcut) kullanılabilir
  kalır — fonksiyonel indekse gerek yoktur.
- Yorum, gerekçesi tersine döndüğü için yeniden yazılır.

#### 2.5.2 `HostType()` — hostname büyük/küçük harf duyarsızlığı

`uq_proxies_endpoint (scheme, host, port, username)` RFC 4343 semantiğine
dayanıyor. Çözüm: `host` **yazılırken** `.lower()`.

**Gerçek yazma yolları (ölçüldü):**
- `scripts/seed_proxies.py:43,78,90` — `parts[0].strip()`, **lower YOK**.
  Asıl risk buradadır.
- `src/yfin/cli.py:795,807` (`yfin proxy add`) — DSN üzerinden gider.
- `src/yfin/proxy/dsn.py:57` — `urlsplit(...).hostname` **zaten küçük
  harfe indirir**; burada yapacak iş yok.
- `src/yfin/proxy/repository.py` — `host` geçen tek satır yok.

Yani düzeltme **`scripts/seed_proxies.py`** ve savunma amaçlı `cli.py`
yolundadır. (v1 bu bölümde yanlış dosyaları gösteriyordu.)

Fonksiyonel `UNIQUE INDEX (scheme, lower(host), port, username)` alternatifi
seçilmedi: tekilliğin biçimini şemaya gömer ve `WHERE host = ?`
sorgularının da elle `lower()` almasını gerektirirdi.

#### 2.5.3 İncelendi, bağımlılık YOK: `insider_transactions`

`datasets/holders/insider_transactions.py:127-129` yorumu `ai_ci`'den
bahsediyor, ama **ters yönde**: *"Hash PYTHON tarafında HAM dizeden
hesaplanır: DB collation'ı `utf8mb4_0900_ai_ci` olduğu için 'Sale'/'sale'
orada aynı görünür, hash'te AYRIŞIR. **Bu bilinçlidir.**"*

Ölçüldü: `insider_transactions` PK'sı `(symbol, start_date, fact_hash)`;
`action` diye bir kolon **yoktur** (en yakını `transaction_label`, PK'da
değil). Yani `'Sale'`/`'sale'` ayrımı **zaten bugün** case-sensitive ve
motor değişimi burada hiçbir şeyi değiştirmiyor.

**Yapılacak tek iş:** yorumdaki `utf8mb4_0900_ai_ci` ifadesi §13(c)
uyarınca PostgreSQL'e göre yeniden yazılır. **Normalizasyon EKLENMEZ** —
`.lower()` uygulamak bilinçli olarak korunan bir ayrımı yok eder ve
bugün ayrı sayılan iki satırı tek satıra indirir. Bu bir gerileme olurdu.

#### 2.5.4 Kalan `ai_ci` bağımlılığı taraması — karar kuralı

Uygulama adım 3'te, yukarıdaki üç yer dışında case-insensitive
karşılaştırmaya dayanan bir yer kalıp kalmadığı taranır. Ölçüt:
`Base.metadata` içindeki her PK/UNIQUE bileşeni olan string kolon için,
o kolona değer yazan normalize yolunun biçim garantisi olup olmadığı.

**Bulgu çıkarsa varsayılan çözüm §2.5.1 desenidir:** normalize katmanında
tek biçime indirgeme (`.upper()` veya `.lower()`), kolon tipi değişmez,
§9.3'e bir regresyon testi eklenir. `CITEXT` ve fonksiyonel UNIQUE
**seçilmez** (§2.5.2 gerekçesi). Bulgu §2.5.3'teki gibi "aslında
bağımlılık değil" çıkarsa yalnızca yorum düzeltilir. Bu kuralla
çözülemeyen bir bulgu uygulamayı durdurur ve yazara sorulur.

**TARAMA YAPILDI (uygulama, Görev 3 Adım 7).** PK/UNIQUE bileşeni olan
117 string kolon incelendi. Bunların ezici çoğunluğu MySQL'de zaten
duyarlıydı (`ascii_bin` veya `utf8mb4_0900_as_cs`); yalnızca tablo
varsayılanı `ai_ci`'yi alan düz `String(n)` kolonları risk taşıyordu.

**Tek bulgu: `lookup_totals.lookup_type`** (PK bileşeni, taban commit'te
düz `String(LOOKUP_TYPE_LENGTH)`).

`company_officers.name` ve `insider_roster.name` de PK bileşenidir ama
`PersonNameType()` = `utf8mb4_0900_as_cs` kullanıyorlardı — **zaten
duyarlıydılar**, davranış değişmiyor.

**Karar: normalizasyon UYGULANMADI**, gerekçe `models/discovery.py`'ye
yazıldı. Değer doğrudan Yahoo yanıtının sözlük anahtarıdır
(`equity`, `mutualfund`, `privateCompany`). İki gerekçe:

1. `privateCompany` camelCase'tir; `.lower()` kaynak tanımlayıcısını
   bozar ve o anahtara göre eşleşen kodu kırar.
2. **Davranış farkı sessiz değil görünürdür.** `ai_ci` altında kaynak bir
   gün `Equity` bildirseydi aynı satır sessizce güncellenirdi; `"C"` ile
   ikinci bir satır oluşur ve denetimde görülür. Projenin tercihi zaten
   gürültülü hatadır (PB§K10 ile aynı ilke).

Bu, §2.5.4'ün karar kuralındaki "bağımlılık değil" dalıdır — ama
§2.5.3'ten farkı, burada davranışın **gerçekten değişmesi**, yalnızca
değişimin daha güvenli yöne olmasıdır.

### 2.6 Unsigned tamsayılar — 35 kolon

PostgreSQL'de unsigned tamsayı yoktur.

| Grup | Kolon sayısı | Yeni tip | CHECK |
|---|---|---|---|
| `ticker_info` / `ticker_info_history` `ubig` alanları | 16 | `BIGINT` | `>= 0` |
| `ticker_fast_info` / `_history` `ubig` alanları | 6 | `BIGINT` | `>= 0` |
| `history_metadata.regular_market_volume` | 1 | `BIGINT` | `>= 0` |
| `price_history.volume`, `price_bars.volume` | 2 | `BIGINT` | `>= 0` |
| `shares_full.shares` (PK bileşeni) | 1 | `BIGINT` | `>= 0` |
| `bar_rescales.rows_affected` | 1 | `BIGINT` | `>= 0` |
| `domain_metrics.employee_count` | 1 | `BIGINT` | `>= 0` |
| `sync_run_items.run_id` (FK), `.proxy_id` | 2 | `BIGINT` | `>= 0` |
| `fund_top_holdings.holding_rank` | 1 | `SMALLINT` | `BETWEEN 0 AND 255` |
| `proxies.port` | 1 | `INTEGER` | `BETWEEN 1 AND 65535` |
| `sync_runs.id`, `sync_run_items.id`, `proxies.id` | 3 | `BIGINT IDENTITY` | — (§2.8) |

**Toplam 35.** İlk 23'ü `kinds.py`'nin `"ubig"` kind'ından gelir
(`make_column`), kalanı elle tanımlıdır. (v1 bu bölümde `domains.employee_count`
diyordu; doğrusu `domain_metrics.employee_count`.)

`kinds.py`'deki `_to_unsigned` dönüştürücüsü (negatifi `None` yapar)
KORUNUR: satır düşürmek yerine hücre düşürme davranışı değişmez ve CHECK
ikinci savunma hattı olur.

**Kısıt adlandırma — `naming_convention` ZORUNLU.** `Base.metadata` şu an
`naming_convention` taşımıyor; adsız kısıtlarda Alembic kararsız adlar
üretir ve §14 adım 7'nin "boş diff" kapısı **hiç açılmaz**. Çözüm,
kısıtları tek tek adlandırmak değil, kaynağı düzeltmektir:

```python
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
```

Bu, `make_column()`'ın imzasını değiştirmeden çalışır: `Field`
(`models/fields.py`) tablo adı taşımıyor ve tek çağrı yeri
`snapshots.py:44`; ad üretimini SQLAlchemy'ye devretmek bu kısıtı ortadan
kaldırır. `make_column()` yalnızca `CheckConstraint(f"{name} >= 0")`
ekler (adsız) — Column'a CheckConstraint iliştirmek `__table_args__`
gerektirmeden **çalışıyor** (ölçüldü).

**Uyarı:** `naming_convention` eklemek MEVCUT tüm indeks/kısıt adlarını
etkiler. Migration sıfırdan üretildiği için (§12) bu bir sorun değil, ama
kodda **açıkça adlandırılmış** indeksler (`ix_price_bars_local_date` vb.)
konvansiyonun ürettiği adla çakışmamalıdır; açık adlar her zaman kazanır.

### 2.7 `password_enc`: `BYTEA`

`BYTEA` `VARBINARY`'nin garantisini verir: baytı olduğu gibi saklar,
collation uygulamaz. `proxy/crypto.py` `bytes` alıp `bytes` döndürüyor;
Python tarafı değişmez. `VARBINARY(512)` uzunluk sınırı kaybolur; sınır
bir doğruluk garantisi değil kaza korumasıydı.

### 2.8 Kimlik kolonları

`BIGINT(unsigned=True), primary_key=True, autoincrement=True`
→ `mapped_column(BigInteger, Identity(always=False), primary_key=True)`.

Ölçüldü: `Identity(always=False)` → `BIGINT GENERATED BY DEFAULT AS IDENTITY`.

**`always=True` seçilmedi.** Gerekçe: `GENERATED ALWAYS` açık `id`
yazımını yasaklar; `BY DEFAULT` ileride bir seed/fixture ihtiyacında
esneklik bırakır. (v1 bunu `scripts/seed_proxies.py`'nin sabit id yazmasına
bağlıyordu — **doğrulanmadı**; `seed_proxies.py:88-95` id yazmıyor.)

**Gerçek risk yazılmalı:** `BY DEFAULT` ile açık `id` yazılırsa IDENTITY
sekansı ilerlemez ve sonraki otomatik değer çakışır. Bu yüzden kod açık
`id` yazmaz; yazan bir yer eklenirse `setval` sorumluluğu o çağıranındır.

`SERIAL` kullanılmaz. `sync_run_items.run_id` / `.proxy_id` yalnızca
`BIGINT`'tir (IDENTITY değil).

### 2.9 ENUM tipleri — 14 tip, 16 kolon

Ölçülen tam liste:

| Tip adı | Kolon(lar) |
|---|---|
| `statement_kind` | `financial_periods.statement`, `financial_facts.statement` |
| `statement_freq` | `financial_periods.freq`, `financial_facts.freq` |
| `estimate_metric` | `analyst_estimates.metric` |
| `holder_type` | `institutional_holders.holder_type` |
| `domain_type` | `domains.domain_type` |
| `domain_fund_type` | `domain_top_funds.fund_type` |
| `domain_rank_type` | `domain_top_movers.rank_type` |
| `fund_section` | `fund_metrics.section` |
| `fund_weight_category` | `fund_weightings.category` |
| `proxyscheme` | `proxies.scheme` |
| `proxyhealth` | `proxies.health` |
| `runscope` | `sync_runs.scope` |
| `runstatus` | `sync_runs.status` |
| `itemstatus` | `sync_run_items.status` |

(v1 "altı" diyordu, 12 ad sayıyordu ve `fund_section` /
`fund_weight_category`'yi atlıyordu; `fund_type`/`rank_type` adları da
yanlıştı.)

**Son beş tip adlandırılır.** `models/proxies.py:_enum()` ve
`models/sync.py`'deki üç `Enum()` çağrısı `name=` vermiyor; SQLAlchemy adı
Python sınıfından türetiyor (`proxyscheme`, `itemstatus`...). Bu bir
**hata değil** — v1'in "adsız bırakılırsa PostgreSQL hata verir" iddiası
yanlıştı; hata yalnızca Python enum sınıfı yerine düz dize listesi
verildiğinde çıkar. Yeniden adlandırma (`proxy_scheme`, `item_status`,
`run_scope`, `run_status`, `proxy_health`) **tutarlılık kararıdır**,
zorunluluk değil.

**Ordinal uyarısı SİLİNİR.** `financials.py`/`analysis.py` şunu diyordu:
*"MySQL native ENUM'da değer sırası ORDINAL'dir ve bileşik FK bu kolonu
taşır; sıra değişirse FK sessizce yanlış satıra bağlanırdı."* Ölçüldü:
PostgreSQL'de değer `pg_enum` OID'i olarak saklanır;
`ALTER TYPE ... ADD VALUE 'x' BEFORE 'y'` sonrası `enumsortorder` 1.5
oldu ve bileşik FK'li satır bozulmadan kaldı.

**Paylaşılan `Enum()` nesnesi (`STATEMENT_ENUM`, `FREQ_ENUM`) KORUNUR**,
ama gerekçesi düzeltilir: neden **tek tanım yeri** ilkesidir — iki ayrı
nesnenin değer listeleri sessizce ayrışabilir. Teknik bir çakışma riski
YOKTUR: SQLAlchemy aynı `MetaData` içinde aynı adlı tipi `checkfirst=False`
ile bile tekilleştiriyor (ölçüldü). v1'in "migration patlar" iddiası
yanlıştı ve tam da §13'ün eleştirdiği sahte gerekçe türündendi.

**Migration uyarısı:** `ALTER TYPE ... ADD VALUE` transaction bloğunda
çalışır ama yeni değer **aynı transaction'da KULLANILAMAZ**
(`unsafe use of new value ... must be committed before they can be used`).
ENUM'a değer ekleyip aynı revizyonda o değeri yazan bir migration iki
revizyona bölünmelidir.

### 2.10 `server_default` değerleri — 14 Boolean kolon

Boolean kolonlarda `server_default="0"` / `"1"` → `text("false")` /
`text("true")`. Tam liste (ölçüldü):

`calendar_earnings.is_known`, `calendar_ipo.is_known`,
`calendar_splits.is_known`, `domain_top_companies.is_known`,
`domain_top_funds.is_known`, `domain_top_movers.is_known`,
`intraday_scope.enabled`, `market_summary.is_known`,
`market_summary_history.is_known`, `news_symbols.is_known`,
`price_bars.is_extended`, `price_history.is_repaired`,
`proxies.is_enabled`, `symbols.is_active`.

(v1 yalnızca 4 tanesini sayıyordu.)

**Gerekçe düzeltildi:** PostgreSQL `'0'`'ı boolean kolonda katalogda zaten
`false`'a normalize eder (ölçüldü), ve `server_default` farkı
`compare_server_default` ile karşılaştırılır — Alembic'te varsayılan
olarak kapalı. Yani teknik bir fark yoktur; `text("false")` **niyeti açık
ifade ettiği için** seçilir.

`Numeric` (`dividend`, `split_ratio`, `capital_gain`) ve `SMALLINT`
(`shard_count`, `shard_index`) `server_default`'ları değişmez.

### 2.11 `ON UPDATE CURRENT_TIMESTAMP` — karşılığı yok

`models/proxies.py:104-108`:

```python
updated_at: Mapped[datetime] = mapped_column(
    TsType(), nullable=False,
    server_default=text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
    server_onupdate=FetchedValue(),
)
```

PostgreSQL'de `ON UPDATE` diye bir kolon cümleciği **yoktur**; DDL
sözdizimi hatası verir. (v1 bu kolonu hiç görmemişti.)

**Karar: Python tarafı `onupdate`.**

```python
updated_at: Mapped[datetime] = mapped_column(
    TsType(), nullable=False,
    server_default=func.now(),
    onupdate=lambda: datetime.now(UTC),
)
```

`server_onupdate=FetchedValue()` kaldırılır. Trigger alternatifi
**seçilmedi**: tek bir kolon için şemaya görünmez bir yan etki eklemek,
projenin "davranış kodda görünür olsun" çizgisine aykırıdır. Maliyeti,
ORM dışından yapılan ham `UPDATE`'lerin bu kolonu tazelememesidir;
`proxies` tablosuna ham UPDATE yazan bir yer yoktur (ölçülecek: adım 3).

---

## 3. Kaldırılan kavramlar

### 3.1 Satır boyutu bütçesi — kaldırılır, yerine bir şey konmaz

Kaldırılanlar: `models/columns.py:MYSQL_ROW_SIZE_LIMIT`,
`estimated_row_size()`; `models/kinds.py:KindSpec.row_cost` ve 14 kind'ın
maliyet değerleri, `_VARCHAR_COST`; `tests/unit/test_fields.py:43
test_row_size_budget_respected`.

**Gerekçe:** PostgreSQL'de 65 535 baytlık satır sınırı yoktur; geniş
değerler TOAST'a taşınır. Pratik sınır kolon sayısıdır (`tables can have
at most 1600 columns`, ölçüldü) ve en geniş tablomuz bunun çok altındadır.

**Yerine bir şey konmaz.** Korunacak bir sınır kalmadığı için ikame bir
savunma icat etmek yanıltıcı olurdu. Bu, migrasyonun bilinçli olarak
kaybettiği tek koruma katmanıdır ve kaybın maliyeti sıfırdır.

`test_fields.py`'deki 5 alan-adı testi KALIR. `KindSpec` üç alandan ikiye
iner.

### 3.2 Partition altyapısının tamamı — kaldırılır, yerine hypertable

Silinecekler:
- `models/bars.py:price_bars_partition_ddl()` ve iki çağıranı (migration,
  `conftest.py`).
- **`src/yfin/maintenance.py` MODÜLÜNÜN TAMAMI** — `LOOKAHEAD_MONTHS`,
  `_month_after`, `_partition_name`, `existing_partitions`,
  `missing_partitions`, `add_partitions`. Üçü de PostgreSQL'de geçersiz:
  `information_schema.partitions` yok, `DATABASE()` yok,
  `ALTER TABLE ... ADD PARTITION` yok. (v1 bu modülü hiç görmemişti.)
- Tek çağıran `src/yfin/cli_bars.py:17,175-183` — `yfin bars maintain`
  komutunun partition ekleme adımı kaldırılır. Komutun kalan işleri
  (öksüz satır raporu, bar_gaps özeti) §9.4 ve §7.2 uyarınca güncellenir.

Birlikte silinen gerekçe metni: `MAXVALUE`/`ERROR 1493`/`ERROR 1526`/
`REORGANIZE PARTITION`. **TimescaleDB chunk'ları otomatik oluşturur**;
"aralık dışı insert" kavramı yoktur, dolayısıyla "gürültülü hata mı sessiz
pruning ölümü mü" ikilemi ortadan kalkar. Migrasyonun en büyük tek kazancı.

### 3.3 `sql_mode` — kaldırılır

`connect_args={"sql_mode": ...}` silinir. Gerekçesi *"gevşek sql_mode'lu
bir sunucuda aralık dışı DECIMAL ve uzun VARCHAR SESSİZCE clamp/truncate
edilir"* idi. PostgreSQL'de bu davranış yoktur (ölçüldü: `numeric(5,2)` ←
`12345.6` → `22003`; uzun varchar → `22001`).

**Ancak:** `NUMERIC(p,s)` PostgreSQL'de de **kesirli** kısmı sessizce
yuvarlar (ölçüldü: `numeric(5,2)` ← `1.239` → `1.24`, uyarı yok) — MySQL
Note 1265 ile aynı. Dolayısıyla *"yuvarlama Python tarafında `quantize`
ile bilinçli yapılır"* kararı (`common.py`, `base.py`) **AYNEN
GEÇERLİDİR** ve korunur; yalnızca "MySQL" sözcüğü "PostgreSQL" olur.
İki davranış (tam sayı taşması = gürültülü hata; kesir = sessiz yuvarlama)
karıştırılmamalıdır.

### 3.4 PK boyut sınırı yorumları — güncellenir

`models/holders.py`'deki *"PK toplamı 548 byte"* ve *"1055 byte (sınır
3072)"* ölçümleri geçerli kalır. PostgreSQL btree indeks **tuple** sınırı
**2704 bayt** (ölçüldü: 2704 OK, 2705 → `index row size 2720 exceeds
btree version 4 maximum 2704`). Sınır tuple boyutudur (header'lar dahil,
~16 bayt ek), payload değil; 548/1055 için etkisiz.

Yorumlar `(sınır 3072)` → `(PostgreSQL btree tuple siniri 2704)` olur.

### 3.5 `ROW_COUNT()` semantiği — `_verify()` korunur

PostgreSQL `ON CONFLICT DO UPDATE` etkilenen satırı doğru sayar ama
`DO NOTHING` ile atlananı saymaz (ölçüldü: `INSERT 0 0`). Anahtar-varlığı
doğrulaması yine daha güçlü bir garanti verir: "kaç satır dokunuldu"yu
değil, "istenen anahtarların kaçı GERÇEKTEN tabloda" sorusunu cevaplar.
Yorum PostgreSQL gerçeğine göre yeniden yazılır; gerekçe artık
`CLIENT_FOUND_ROWS` değil, doğrulamanın yazma-sonrası bağımsız bir okuma
olmasıdır.

---

## 4. `persistence.py` → `PostgresRowWriter`

Yeniden adlandırma **16 dosyayı** etkiler (ölçüldü; otorite
`grep -rl MySQLRowWriter src tests`):

`src/`: `persistence.py`, `runner.py`, `market_runner.py`, `domain_runner.py`
`tests/`: `domain_support.py`, `live/test_live_bars.py`,
`repo/test_asof_gate.py`, `repo/test_dataset_writes.py`,
`repo/test_domain_prune.py`, `repo/test_domain_scope.py`,
`repo/test_financials_repo.py`, `repo/test_fixture_writes.py`,
`repo/test_prune_asof.py`, `repo/test_repository.py`,
`repo/test_shard_writes_repo.py`, `unit/test_insert_chunk.py`.

(v1 "6 kullanım noktası" diyordu; liste izlenirse ağaç import hatalarıyla
dolu kalırdı.)

`RowSink` / `HashReader` / `SymbolLookup` / `SnapshotWriter` / `RowWriter`
protokolleri **değişmez** — bu ISP ayrımı motordan bağımsızdır ve
`datasets/` altındaki 40+ dosyanın hiç değişmemesini sağlayan şeydir.

### 4.1 `ON CONFLICT` hedefi

```python
from sqlalchemy.dialects.postgresql import insert as pg_insert

stmt = pg_insert(table).values(rows)
...
if update_map:
    return stmt.on_conflict_do_update(
        index_elements=list(write.key_columns), set_=update_map
    )
return stmt.on_conflict_do_nothing(index_elements=list(write.key_columns))
```

`stmt.inserted[col]` → `stmt.excluded[col]`.

**`index_elements` semantiği (ölçüldü).** Küme olarak **tam eşleşmeli**;
sıra önemsiz, ama alt küme de üst küme de reddedilir:

```
PK (a,b):  on conflict (b,a)   -> OK
           on conflict (a)     -> ERROR: no unique or exclusion constraint matching
           on conflict (a,b,c) -> ERROR: aynı
```

Kısmi (partial) unique indeks hedeflenirse `DO UPDATE` için predicate
**zorunludur** (`index_where=`), `DO NOTHING` için değil. Bugün kısmi
unique indeks yok; ileride eklenirse bu çalışma zamanında patlar.

**İki yapısal iyileşme.** (a) Çakışma hedefi artık açık — MySQL "herhangi
bir unique anahtar" diyordu, hangi kısıtın tetiklendiği görünmezdi.
(b) `DO NOTHING` sayesinde MySQL'in `first_key = first_key` hilesi ve
onu açıklayan yorum silinir.

### 4.1.1 Dilim içi anahtar tekrarı — YENİ savunma (zorunlu)

**PostgreSQL `ON CONFLICT DO UPDATE` aynı komutta aynı satıra iki kez
dokunamaz:** `21000 cardinality_violation — ON CONFLICT DO UPDATE command
cannot affect row a second time`. MySQL `ON DUPLICATE KEY UPDATE` bunu
sorunsuz yutuyordu.

`_insert_stmt` `INSERT_CHUNK = 2000` satırlık toplu insert üretiyor.
Bazı dataset'ler kendi içinde `drop_duplicates` yapıyor (4 yerde) ama
57 `TableWrite` çağrısının tamamı için garanti yok. **Spec uygulanırsa
üretimde patlayacak en olası yer burasıdır.**

**Çözüm:** `PostgresRowWriter.write()` içinde, `align_rows()`'dan SONRA
ve dilimlemeden ÖNCE `key_columns` üzerinden dedupe:

- Varsayılan **son kazanır** (kaynak sıralaması otoritedir).
- `monotonic_columns` istisnadır: grup içindeki **max** alınır. Aksi
  halde monotoniklik dilim içinde geri yazılabilirdi — `GREATEST`
  yalnızca mevcut DB satırıyla yeni satırı karşılaştırır, aynı batch'teki
  iki satırı değil.
- Düşürülen satır sayısı `WriteStats` üzerinden görünür kalmalıdır:
  `attempted` ham satır sayısını, `verified` doğrulanmış anahtar sayısını
  saymaya devam eder; dedupe ikisinin arasındaki farkı açıklar.

Bu, `align_rows()`'un yanına konan ikinci bir hizalama adımıdır ve aynı
gerekçeyi taşır: dilimlemeden önce yapılmazsa her dilim farklı davranır.

### 4.2 `GREATEST` — R1 kapandı

`func.greatest(table.c[col], stmt.excluded[col])` korunur.

Ölçüldü: PostgreSQL `GREATEST(x, NULL) = x` (NULL yok sayılır; MySQL
`NULL` döndürüyordu), `GREATEST(false, true) = true`,
`GREATEST(true, NULL::boolean) = true`. Hypertable üzerinde
`ON CONFLICT DO UPDATE ... greatest(...)` monotonikliği koruyor.

**Yedek plan (mantıksal `OR`) gereksizdir ve spec'ten çıkarıldı.**
PostgreSQL davranışı burada MySQL'den **daha güvenlidir**: kaynak bir kez
NULL bildirse bile mevcut `true` korunur.

Tek monotonik kolon `price_history.is_repaired` (`MONOTONIC_COLUMNS`,
`nullable=False` — ölçüldü). Fark yorum olarak yazılır ve
`test_insert_chunk.py`'de üretilen SQL üzerinden sabitlenir.

### 4.3 Değişmeyenler

`INSERT_CHUNK = 2000` (iki gerekçe de motordan bağımsız;
`max_allowed_packet` referansı silinir), `align_rows()`, `_delete_scope()`
(`tuple_(...).in_(...)` PostgreSQL'de satır karşılaştırmasıdır),
`_verify()` ve `VERIFY_CHUNK = 500` (`range_optimizer_max_mem_size`
referansı silinir), `current_hash()`, `known_symbols()`.

### 4.4 Sürücü

`pymysql>=1.1` → `psycopg[binary]>=3.2`, dialect `postgresql+psycopg`.
`psycopg2` kullanılmaz (bakım modunda).

---

## 5. `db.py` — advisory lock ve engine

### 5.1 Kilit anahtarı

```python
def _lock_key(name: str) -> int:
    """PG bigint advisory anahtari (imzali 64-bit)."""
    digest = hashlib.blake2b(name.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def _lock_key_parts(key: int) -> tuple[int, int]:
    """pg_locks.classid / objid (ikisi de oid = isaretsiz 32-bit)."""
    unsigned = key & 0xFFFF_FFFF_FFFF_FFFF
    return unsigned >> 32, unsigned & 0xFFFF_FFFF
```

PostgreSQL'in `hashtext()`'i **kullanılmaz**: dokümante edilmemiş bir iç
fonksiyondur ve dönüş değeri sürümler arasında değişebilir.

**Ayrıştırma neden Python'da:** `pg_locks.classid`/`objid` `oid` tipidir
(işaretsiz 32-bit) ve anahtar imzalıdır. SQL içinde `(:key >> 32)::int`
yapmak iki ayrı hata üretir (ikisi de ölçüldü):
- Alt 32 bit ≥ 2³¹ olduğunda `::int` cast'i **`22003 integer out of
  range`** fırlatır.
- Negatif anahtarda `>>` aritmetik kaydırmadır, işareti uzatır: gerçek
  `classid` `2896445778` iken sorgu `-1398521518` üretir.

Bu teorik bir risk değildir. `SYNC_LOCK_NAME = "yfin_sync"` için gerçek
anahtar hesaplandı:

```
anahtar (signed bigint) = -5176397690111661269   -> NEGATIF
classid                 =  3089743290            -> 2^31'in USTUNDE
objid                   =    12446507
```

Yani v1'in SQL bloğu **projenin tek advisory kilidinde, ilk gerçek
kullanımda** `22003` ile patlardı: hem negatif anahtar hem de 2³¹'i aşan
`classid` koşulu aynı anda sağlanıyor. Üstelik bu, `LockNotAcquired`
hata yolunun İÇİNDE olduğu için asıl hatayı maskeleyecekti.
Ayrıştırmayı Python'a almak SQL'i de okunaklı kılar.

### 5.2 Alma ve bırakma

| MySQL | PostgreSQL |
|---|---|
| `SELECT GET_LOCK(:n, 0)` | `SELECT pg_try_advisory_lock(:key)` |
| `SELECT RELEASE_LOCK(:n)` | `SELECT pg_advisory_unlock(:key)` |

`GET_LOCK(name, 0)` = "bekleme, hemen dön" — `pg_try_advisory_lock` ile
aynı semantik. Kilit oturum kapsamlıdır; `engine.connect()` ile alınan tek
bağlantı `finally` bloğuna kadar açık tutulur.

`advisory_lock()` imzasındaki `timeout: int = 0` **kaldırılır**: 5
çağıranın hiçbiri değer vermiyor (ölçüldü) ve PostgreSQL'de karşılığı
saniye değil, `pg_advisory_lock` (sonsuz bekleme) ile
`pg_try_advisory_lock` (hiç beklememe) arasında ikili bir seçimdir.

### 5.2.1 KAPSAM FARKI — MySQL sunucu geneli, PostgreSQL veritabanı bazlı

**Bu, semantiğin birebir aynı OLMADIĞI tek yerdir.** MySQL `GET_LOCK`
tüm sunucuda tektir: `yfinance` veritabanında koşan bir `yfin sync` ile
`yfinance_test` veritabanında koşan bir live test **birbirini görür**.
PostgreSQL advisory kilitleri **veritabanı kapsamlıdır**; §9.1 test
veritabanını ayrı tuttuğu için bu iki kilit artık birbirini görmez.

Somut kırılan yer: `tests/conftest.py:136` `_guard_concurrent_live_runs`,
`create_engine(get_settings().db_url())` ile **canlı** veritabanına
bağlanıp kilidi kontrol ediyor; live testler ise test veritabanında kilit
alıyor. Naif çeviri bu guard'ı sessizce işlevsiz bırakırdı.

**Karar:** live testler **canlı veritabanına** karşı koşar (bugün de
öyle: `test_live_*` `run_sync` çağırıyor ve o `db_url()` kullanıyor).
Guard da canlı veritabanına bağlanmaya devam eder — yani kilit ve guard
**aynı veritabanındadır** ve davranış korunur. §9.1'in şema izolasyonu
`-m repo` testleri içindir; `-m live` bundan etkilenmez.

Bu ayrım §9.1'de açıkça tekrarlanır, çünkü "test veritabanı ayrı" ilkesi
ile "live testler canlıya yazar" gerçeği ilk bakışta çelişik görünür.

### 5.3 "Kim tutuyor" teşhisi — iyileşiyor

```sql
SELECT a.pid, a.application_name, a.client_addr,
       a.backend_start, a.state, left(a.query, 120) AS query
  FROM pg_locks l
  JOIN pg_stat_activity a ON a.pid = l.pid
 WHERE l.locktype = 'advisory'
   AND l.classid  = :classid
   AND l.objid    = :objid
   AND l.objsubid = 1
   AND l.granted
```

`:classid` / `:objid` `_lock_key_parts()`'tan gelir (§5.1).

**`objsubid = 1` kritiktir (ölçüldü):** tek `bigint` argümanlı advisory
kilitler `objsubid = 1`, iki `int` argümanlı biçim `objsubid = 2`
kullanır. Yanlış `objsubid` ile sorgu sessizce boş döner.

MySQL `IS_USED_LOCK` yalnızca connection id veriyordu ve mesaj
`SHOW PROCESSLIST` tavsiye ediyordu; artık pid, `application_name` ve
çalışan sorgu doğrudan `LockNotAcquired` mesajına girer.

`tests/conftest.py:_guard_concurrent_live_runs` (`IS_FREE_LOCK` /
`IS_USED_LOCK`) aynı sorguya çevrilir: satır dönerse kilit meşguldür.

### 5.4 Engine ayarları ve imza değişikliği

```python
def create_db_engine(
    settings: Settings | None = None,
    database: str | None = None,
    *,
    schema: str | None = None,      # YENI (§9.1)
    pool_size: int | None = None,
    application_name: str = "yfin",
) -> Engine:
    ...
    options = ["-c timezone=UTC"]
    if schema is not None:
        options.append(f"-c search_path={schema},public")
    return create_engine(
        cfg.db_url(database),
        pool_pre_ping=True, pool_recycle=3600,
        pool_size=pool_size, max_overflow=pool_size, pool_timeout=60,
        future=True,
        connect_args={
            "application_name": application_name,
            "options": " ".join(options),
        },
    )
```

**`options` TEK BİR DİZEDİR.** v1 §5.4'te `-c timezone=UTC`, §9.1'de
`-csearch_path=...` yazıyordu; ikisi ayrı ayrı verilseydi biri diğerini
ezerdi. Birleştirme burada tek yerde yapılır.

**`schema` parametresi yenidir** ve §9.1'in şema izolasyonunu mümkün
kılar. Mevcut çağıranlar (`tests/conftest.py:51`, `src/yfin/shard.py:30`,
`scripts/seed_proxies.py:29`) `database` konumsal argümanını kullanmaya
devam eder; yalnızca conftest `schema=` ekler.

- `sql_mode` gider (§3.3).
- `application_name` shard'larda indeksle zenginleşir (`yfin-shard-2`),
  böylece kilidi hangi shard'ın tuttuğu doğrudan görünür.
- Havuz formülü `max(5, workers + 4)` **değişmez**; yorumdaki dört eş
  zamanlı tüketici sayımı motordan bağımsızdır. `MySQL max_connections`
  → `PostgreSQL max_connections` ve §1'deki 200'e atıf.

Ölçüldü: `connect_args={"application_name": ..., "options": "-c timezone=UTC"}`
psycopg3 ile çalışıyor (`show timezone` → `UTC`,
`pg_stat_activity.application_name` → `yfin`).

---

## 6. Kilit çakışması ve yeniden deneme

`src/yfin/runner.py:612-621` (`_LOCK_ERRORS`, `_is_lock_conflict`) ve
`:629-632` docstring:

```python
# 40001 serialization_failure, 40P01 deadlock_detected
_RETRYABLE_SQLSTATES = frozenset({"40001", "40P01"})

def _is_lock_conflict(exc: BaseException) -> bool:
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    return sqlstate in _RETRYABLE_SQLSTATES
```

Mevcut kod hata **metninde** `"1213"`, `"1205"`, `"deadlock"`,
`"lock wait timeout"` arıyordu. SQLSTATE tabanlı kontrol yapısal olarak
daha güvenilirdir. Ölçüldü: `exc.orig.sqlstate` doğru erişim yolu;
`40001` = `SerializationFailure`, `40P01` = `DeadlockDetected`.

**`55P03` (`lock_not_available`) LİSTEDEN ÇIKARILDI.** v1 onu ekliyordu
ama bu kod yolunda hiç oluşmuyor: `NOWAIT` / `SKIP LOCKED` kullanılmıyor.
Gerekçesiz bir SQLSTATE'i yeniden denemek, ileride gerçekten `NOWAIT`
eklenirse yanlış davranışı sessizce meşrulaştırırdı.

**`innodb_rollback_on_timeout` notu silinir.** PostgreSQL'de hata alan
transaction **her zaman** abort durumuna geçer ve `ROLLBACK` dışında
komut kabul etmez. Mevcut kod zaten `session.rollback()` çağırıyor →
**davranış doğru kalır**, yalnızca gerekçe "motor bunu zorunlu kılıyor"
olur.

`yf_txn_retry_attempts` ve jitter'lı bekleme değişmez.

---

## 7. TimescaleDB

### 7.1 Hypertable'lar

| Tablo | Zaman kolonu | Tip | `chunk_time_interval` |
|---|---|---|---|
| `price_bars` | `ts_utc` | `TIMESTAMPTZ(6)` | `INTERVAL '7 days'` |
| `price_history` | `session_date` | `DATE` | `INTERVAL '365 days'` |

```sql
SELECT create_hypertable('price_bars',
       by_range('ts_utc', INTERVAL '7 days'),
       create_default_indexes => FALSE);
SELECT create_hypertable('price_history',
       by_range('session_date', INTERVAL '365 days'),
       create_default_indexes => FALSE);
```

**`create_default_indexes => FALSE` ZORUNLUDUR (v1'de yoktu).** Varsayılan
davranış bölümleme kolonu üzerinde `price_bars_ts_utc_idx` /
`price_history_session_date_idx` adlı bir DESC indeks yaratır. Bu indeks
`public` şemasında durur, `Base.metadata`'da yoktur ve Alembic autogenerate
onu **"silinmeli" diye raporlar** (ölçüldü: temiz bir veritabanında tek
fark olarak `op.drop_index(...)` üretti). §14 adım 7 ve §15/5'in "boş
diff" kapısı bu bayrak olmadan **hiç açılmaz**; `include_object` filtresi
de yakalamaz, çünkü indeks `public` şemasındadır.

İhtiyaç duyulan indeksler modelde açıkça tanımlanır. `price_bars` zaten
`ix_price_bars_local_date` taşıyor; `ts_utc` PK'nın son bileşeni olduğu
için ayrı bir indeks gerekmez. `price_history`'de `session_date` üzerinde
zaten `ix_price_history_session_date` var.

**Ön koşullar (ölçüldü, üçü de sağlanıyor):**
1. Bölümleme kolonu `NOT NULL` — ikisi de PK bileşeni.
2. Her `UNIQUE`/`PK` indeksi bölümleme kolonunu içermeli. Aksi halde:
   `ERROR: cannot create a unique index without the column "ts_utc"
   (used in partitioning)`. `price_bars` PK `(symbol, bar_interval, ts_utc)` ✓,
   `price_history` PK `(symbol, session_date)` ✓; başka UNIQUE yok ✓.
3. Tablo boş olmalı — migration yeni oluşturur.

**`DATE` kolonda `by_range(col, INTERVAL)` kabul ediliyor** (ölçüldü);
v1'deki R2 riski kapandı.

**`INTERVAL '1 year'` KULLANILMAZ:** TimescaleDB ay içeren interval'ı 30
günlük aylara çevirir ve `time_interval` **360 gün** olarak kaydolur
(ölçüldü). Kesin 365 istendiği için `INTERVAL '365 days'` yazılır.

**Chunk aralığı gerekçeleri.** `price_bars`: PB§5.2'ye göre ilk yıl sonunda
~464 milyon satır, ağırlık `1m` barlarda; 7 gün, günlük yazma penceresinin
bir chunk'a düşmesini ve `rescale` UPDATE'inin sınırlı sayıda chunk'a
dokunmasını sağlar. `price_history`: sembol başına yılda ~252 satır,
5 000 sembolde ~1,26 milyon; 365 günlük chunk küçük kalır ve chunk
sayısını büyütmez.

**Bunlar ölçülmemiş başlangıç değerleridir** ve `set_chunk_time_interval()`
ile sonradan değiştirilebilir (yalnızca yeni chunk'ları etkiler).
**Kabul ölçütü (ölçüm gerektirmez):** `-m repo` koşusundan sonra
`timescaledb_information.chunks` her iki hypertable için de >0 ve <100
chunk göstermelidir. Gerçek veri hacmiyle ayarlama ayrı bir karardır.

**Hash boyutu (`add_dimension(by_hash(...))`) EKLENMEZ.** Ölçüm olmadan
gerekçesi yoktur ve chunk sayısını N katına çıkarır.

### 7.2 `price_bars`'a foreign key geri geliyor

`models/bars.py` şu an: *"FK YOKTUR: MySQL 8 partition'lı InnoDB tablosunda
foreign key desteklemez (ERROR 1506)."*

**Ölçüldü ve kapandı:** TimescaleDB hypertable'dan normal tabloya FK
çalışıyor; `ON UPDATE CASCADE` ve `ON DELETE RESTRICT` ikisi de kabul
ediliyor; yalnızca giden FK varken `drop_chunks` sorunsuz çalışıyor.
`price_bars.symbol` artık `symbol_fk_column()` kullanır.

Sonuçlar:
- `models/bars.py`'deki FK'siz kolon tanımı ve gerekçesi silinir.
- PB§7.5/2'nin aylık **öksüz satır denetim sorgusu gereksizleşir**.
  Konumu: **`src/yfin/cli_bars.py:187-196`** (v1 yanlışlıkla
  `maintenance.py` diyordu — orada böyle bir sorgu yok).
- `tests/unit/test_schema_invariants.py:test_every_symbol_fk_uses_the_same_policy`
  artık `price_bars`'ı da kapsar — invaryantın kapsamı **genişler**.

**Kabul edilen maliyet:** her insert `symbols` satırında paylaşımlı kilit
alır. `price_bars` en yoğun yazılan tablodur; maliyet `-m repo` ve
`-m live` koşularında gözlenir. Kabul edilemez bir yavaşlama görülürse FK
geri alınır ve **bu doküman güncellenir** — sessizce bırakılmaz.

### 7.3 Compression AÇILMAZ

`rescale.py` geriye dönük split düzeltmesi için **eski** `price_bars`
satırlarını `UPDATE` eder (`WHERE ts_utc < :boundary`). Sıkıştırılmış
chunk'ta `UPDATE` desteklenir ama chunk'ı açmayı gerektirir ve pahalıdır;
bir split tüm tarihsel arşive dokunabilir.

Bu bir motor değişimi migrasyonudur; compression ayrı bir optimizasyon
kararıdır ve ölçüm ister. Altyapı kurulduktan sonra tek bir
`ALTER TABLE ... SET (timescaledb.compress...)` + `add_compression_policy()`
ile eklenir. **Eksiklik değil, bilinçli erteleme.**

### 7.4 Retention policy AÇILMAZ, `prune.py` DEĞİŞMEZ

`add_retention_policy()` kullanılmaz. Gerekçe: `bar_gaps.reason =
'retention_expired'` kaydını **uygulama** yazar (`prune.py`). PB§K10'un
ilkesi şudur: kalıcı arşivde "veri yok" iki farklı şey olabilir — piyasa
kapalıydı, ya da biz kaçırdık; bu ayrım sonradan yapılamaz. Otomatik bir
policy chunk'ı arka planda sessizce düşürürse `bar_gaps` satırı hiç
yazılmaz ve ayrım kaybolur.

**Karar: bu migrasyonda `prune.py` DEĞİŞMEZ.** Mevcut satır satır `DELETE`
mantığı korunur (ölçüldü: `prune.py` ham SQL içermiyor, saf SQLAlchemy
Core — motor değişiminden etkilenmiyor). `drop_chunks()` optimizasyonu
kapsam dışıdır ve ayrı bir kararla ele alınır. (v1 bunu "isterse
çağırabilir" diye açık bırakıyordu.)

### 7.5 Continuous aggregate YOK

Kapsam dışı (YAGNI). `v_price_bars_regular` düz `VIEW` kalır (§8).

### 7.6 DDL'in yeri

`create_hypertable` çağrılarını Alembic autogenerate edemez. Mevcut
`price_bars_partition_ddl()` deseni aynen korunur: `models/bars.py` içinde
`timescale_ddl() -> tuple[str, ...]` tanımlanır; hem initial migration hem
`tests/conftest.py` **bu aynı sabiti** kullanır. Aksi halde testler
hypertable'sız düz tablolara karşı koşar ve chunk davranışı hiç
doğrulanmaz.

`CREATE EXTENSION IF NOT EXISTS timescaledb` **veritabanı düzeyindedir**,
şema başına tekrarlanmaz; `db create`'in ürünüdür (§10).

---

## 8. View'lar

```sql
CREATE OR REPLACE VIEW v_actions
  WITH (security_invoker = true) AS
  SELECT symbol, ex_date AS action_date,
         CAST('DIVIDEND' AS VARCHAR(16)) AS action_type,
         amount AS action_value FROM dividends
  UNION ALL
  SELECT symbol, split_date, CAST('SPLIT'        AS VARCHAR(16)), ratio  FROM splits
  UNION ALL
  SELECT symbol, gain_date,  CAST('CAPITAL_GAIN' AS VARCHAR(16)), amount FROM capital_gains
```

```sql
CREATE OR REPLACE VIEW v_price_bars_regular
  WITH (security_invoker = true) AS
  SELECT symbol, bar_interval, ts_utc, local_date,
         open, high, low, close, volume
    FROM price_bars
   WHERE is_extended = false
```

- `security_invoker` ölçüldü: `reloptions = {security_invoker=true}`,
  PG 15+ gereksinimi doğru, 18.6'da çalışıyor.
- `CAST(... AS CHAR(16))` → `VARCHAR(16)`. Gerekçe korunur: PostgreSQL'de
  tırnaklı literal `unknown` tipindedir ve `UNION` içinde `text`e çözülür;
  açık cast yeni bir action türünde kolon tipinin sessizce değişmesini
  engeller. `CHAR(16)` PostgreSQL'de `bpchar`tır ve **sonda boşluk
  doldurur** — `VARCHAR` doğru karşılıktır.
- `is_extended = 0` → `= false`.

Ölçüldü: hypertable üzerindeki düz view'da chunk exclusion çalışıyor
(`Custom Scan (ChunkAppend)`, `Index Cond` chunk seviyesine iniyor).

---

## 9. Test altyapısı

### 9.1 Süreç izolasyonu: veritabanı → şema

| | MySQL | PostgreSQL |
|---|---|---|
| Oluşturma | `CREATE DATABASE \`yfinance_test_<pid>\`` | `CREATE SCHEMA "yfinance_test_<pid>"` |
| Silme | `DROP DATABASE IF EXISTS` | `DROP SCHEMA IF EXISTS ... CASCADE` |
| Bağlanma | URL'de veritabanı adı | `create_db_engine(..., schema=...)` (§5.4) |
| Bayat temizlik | `information_schema.SCHEMATA` / `SCHEMA_NAME` | `information_schema.schemata` / `schema_name` |

`CREATE DATABASE` PostgreSQL'de transaction dışında çalışmak zorundadır,
şablon veritabanını kopyalar ve pahalıdır; `CREATE SCHEMA` sıradan bir
DDL'dir.

**`search_path` içinde `public` ZORUNLUDUR.** Ölçüldü: `public` olmadan
`ERROR: function by_range(unknown, interval) does not exist`. Eklenti
`public` şemasına kurulur; `create_hypertable` ve
`timescaledb_information.*` görünümleri bunun için arama yolunda olmalıdır.

**İKİ AYRI ENGINE (v1'in kritik hatası).** PostgreSQL'de
`information_schema.schemata` **veritabanına özeldir**: `postgres` bakım
veritabanına bağlı bir engine `yfinance_test` içindeki pid şemalarını
**asla göremez**. v1 `drop_stale_schemas`'ı `bootstrap` engine ile
çağırmayı sürdürüyordu → temizlik sessizce hiçbir şey yapmaz, şemalar
sonsuza kadar birikirdi.

`tests/conftest.py` fixture'ları ikiye ayrılır:
- `bootstrap_engine` → `bootstrap_url()` (`postgres`). **YALNIZCA**
  `CREATE DATABASE` içindir; testlerde normalde hiç kullanılmaz.
- `test_db_engine` → `db_url(db_test_name)`. Şema oluşturma, silme ve
  `drop_stale_schemas()` bunu kullanır.

`helpers.py:schema_name()` ve PID canlılık kontrollü
`drop_stale_schemas()` **mantığı** korunur; yalnızca SQL'i değişir
(`SELECT schema_name FROM information_schema.schemata`,
`DROP SCHEMA IF EXISTS "..." CASCADE`).

**`DROP SCHEMA ... CASCADE` chunk'ları da temizler** (ölçüldü:
`drop cascades to table _timescaledb_internal._hyper_1_1_chunk`,
sonrasında `chunks_left = 0`). Chunk sızıntısı riski yoktur.

**Tablolar `Base.metadata.create_all()` + `timescale_ddl()` ile kurulur;
Alembic testlerde koşmaz.** Bu, §14 adım 7'deki "boş diff" koşulunun neden
migration/model ayrışmasını yakalayan **tek** kontrol olduğunu açıklar.

**`-m live` bundan etkilenmez** (§5.2.1): live testler canlı veritabanına
karşı koşar ve advisory kilidi orada alır; guard da oraya bağlanır.
Şema izolasyonu `-m repo` içindir.

`db_session` (bağlantı kapsamlı transaction + rollback) ve
`committed_session` fixture'ları değişmez. `cleanup_tables`'daki
backtick'ler çift tırnağa döner.

### 9.2 Yeni invaryant: `key_columns` ↔ PK/UNIQUE

`tests/unit/test_persistence_contract.py`'ye eklenir (dosya zaten
"MySQL'e HİÇ dokunmaz" ilkesiyle yazılmış). Her dataset'in ürettiği her
`TableWrite` için `set(key_columns)` o tablonun PK kolon kümesine **veya**
bir UNIQUE kısıt kümesine eşit olmalıdır — §4.1'in `index_elements` tam
eşleşme kuralının ön koşulu.

### 9.3 Yeni regresyon testleri

§2.5'te kaldırılan case-insensitive semantiğin **her biri** için:

1. `symbols` filtresi: `--exchange nms` ile `--exchange NMS` aynı sonucu
   döndürmeli (§2.5.1).
2. `proxies`: `HOST.example.com` ve `host.example.com` ile iki kez ekleme
   `uq_proxies_endpoint` ihlali vermeli — test **`scripts/seed_proxies.py`
   yolunu** kullanmalı (§2.5.2).

v1'deki üçüncü test (`insider_transactions` 'Sale'/'sale' tek satır)
**KALDIRILDI**: mevcut ve doğru davranışı bozardı (§2.5.3).

Ek olarak §4.1.1 için: aynı `TableWrite` içinde tekrar eden anahtar
verildiğinde `write()` `cardinality_violation` üretmemeli ve son değer
kazanmalı; monotonik kolonda grup max'ı alınmalı.

### 9.4 Uyarlanan testler ve ham SQL

**Mekanik (§13(a) kapsamı):**

| Dosya:satır | Bulgu | Karşılık |
|---|---|---|
| `tests/repo/test_advisory_lock.py:24,28,55` | `IS_FREE_LOCK` | `pg_locks` sorgusu (§5.3) |
| `tests/repo/test_bars_schema.py:44,63` | `information_schema.partitions` | `timescaledb_information.dimensions` / `.chunks` |
| `tests/repo/test_domain_audit.py:69` | `SELECT LAST_INSERT_ID()` | `INSERT ... RETURNING id` |
| `tests/repo/test_repository.py:295` | `SHA2(raw_json, 256)` | `encode(sha256(raw_json::bytea), 'hex')` |
| `tests/live/test_live_sync.py:168` | `SHA2(...)` | aynı |
| `tests/repo/test_financials_repo.py:286,626-628` | `COLUMN_TYPE`, `DATABASE()` | `pg_enum`/`pg_type` üzerinden ENUM etiketleri |
| `tests/repo/test_domain_schema.py:132,144` | `COLUMN_TYPE`, `KEY_COLUMN_USAGE`, `DATABASE()` | `pg_enum` + `pg_constraint` |
| `tests/repo/test_financials_repo.py:656,664` | `CREATE DATABASE IF NOT EXISTS`, `SCHEMA_NAME` | `CREATE SCHEMA`, `schema_name` |
| `tests/helpers.py:54,60` | `SCHEMA_NAME`, `DROP DATABASE` | `schema_name`, `DROP SCHEMA ... CASCADE` |
| `tests/repo/test_analysis_schema.py:128` | backtick'li kolon listesi üreten yardımcı | çift tırnak |
| `tests/repo/test_domain_writes.py:161` | `information_schema.COLUMNS` | `columns`, kolon adı `column_name` |
| `src/yfin/cli_bars.py:212` | `SUM(resolved_at IS NULL)` | `COUNT(*) FILTER (WHERE resolved_at IS NULL)` |
| `src/yfin/rescale.py:218-221` | `INSERT ... ON DUPLICATE KEY UPDATE symbol = symbol` (slot alma) | `ON CONFLICT DO NOTHING`; `rowcount` semantiği korunur (1 = slot bizim, 0 = başkası aldı) |
| `src/yfin/rescale.py:242-253` | `FLOOR(volume * :factor)` | PostgreSQL'de çalışır, `numeric` döner, `bigint` kolona atanır. UPDATE artık hypertable'a gider |

**Backtick taraması otoritedir:** `grep -rn '`' src tests`.

**Model/invaryant testleri:**

| Dosya | Değişiklik |
|---|---|
| `tests/unit/test_schema_invariants.py` | `dialects.mysql.DATETIME(fsp=6)` → `dialects.postgresql.TIMESTAMP(timezone=True, precision=6)`. Collation invaryantı somutlaşır: **her symbol-FK çiftinin iki ucunun `collation` özniteliği eşit olmalı** |
| `tests/unit/test_fields.py` | `test_row_size_budget_respected` **silinir**; 5 alan-adı testi kalır |
| `tests/unit/test_insert_chunk.py` | `PostgresRowWriter`; üretilen SQL `ON CONFLICT` beklentisi; `GREATEST` NULL farkı; §4.1.1 dedupe |

`pyproject.toml` marker açıklamaları: "gercek MySQL gerektirir" → "gercek
PostgreSQL/TimescaleDB gerektirir".

**Kolon adı tuzağı:** PostgreSQL tırnaksız tanımlayıcıları küçük harfe
indirir. Projedeki tüm tablo/kolon adları zaten `snake_case` küçük
harftir → etkilenmez. Ancak `information_schema` **sonuç kolonları**
MySQL'de büyük, PostgreSQL'de küçük harflidir.

---

## 10. Yapılandırma

| Ayar | Eski | Yeni |
|---|---|---|
| `db_port` | `3306` | `5432` (`.env` `DB_PORT`, §1) |
| `db_user` | `"root"` | `"yfin"` |
| `db_password` | `""` | `.env`'den zorunlu |
| `db_name` | `"yfinance"` | değişmez |
| `db_test_name` | `"yfinance_test"` | değişmez — ayrı **veritabanı**, içinde pid şemaları (§9.1) |

```python
def db_url(self, database: str | None = None) -> URL:
    return URL.create(
        drivername="postgresql+psycopg",
        username=self.db_user, password=self.db_password,
        host=self.db_host, port=self.db_port,
        database=database if database is not None else self.db_name,
    )

def bootstrap_url(self) -> URL:
    """YALNIZCA CREATE DATABASE icin bakim veritabani (§9.1)."""
    return self.db_url(database="postgres")
```

`query={"charset": "utf8mb4"}` silinir.

`cli.py:db_create`:

```python
# CREATE DATABASE transaction icinde CALISMAZ -> AUTOCOMMIT sart
engine = create_engine(settings.bootstrap_url(), isolation_level="AUTOCOMMIT")
with engine.connect() as conn:
    for name in (settings.db_name, settings.db_test_name):
        # CREATE DATABASE IF NOT EXISTS PostgreSQL'de YOKTUR
        if not conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
        ).scalar():
            conn.execute(text(f'CREATE DATABASE "{name}"'))
# Eklenti HER veritabaninda AYRI kurulur
for name in (settings.db_name, settings.db_test_name):
    with create_engine(
        settings.db_url(name), isolation_level="AUTOCOMMIT"
    ).connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
```

Üçü de ölçüldü: `CREATE DATABASE cannot run inside a transaction block`;
`IF NOT EXISTS` yok; `CREATE EXTENSION IF NOT EXISTS` her veritabanında
ayrıdır (bu imajda `template1`'e önceden kurulu olduğu için idempotent).

`.env.example` bu anahtarlarla güncellenir; `DB_PASSWORD` zorunlu
işaretlenir.

---

## 11. Paket ve meta

- `pymysql>=1.1` → `psycopg[binary]>=3.2`
- `description`: `"yfinance Ticker API -> MySQL 8 veri hatti"` →
  `"... -> PostgreSQL 18 + TimescaleDB veri hatti"`
- pytest marker açıklamaları (§9.4)
- `src/yfin/__init__.py` ve `cli.py:45` docstring/yardım metni
- `ruff` / `mypy` yapılandırması değişmez
- `alembic.ini` **değişmez** (MySQL referansı yok — doğrulandı)

**Yeniden kurulum zorunlu:** `src/yfin.egg-info/` build artefaktı eski
`Summary` ve `Requires-Dist: pymysql` satırlarını taşır ve `src/` altında
olduğu için §15/6 taramasına takılır. Adım 11'de `pip install -e .`
(veya `uv sync`) çalıştırılır.

---

## 12. Migration

`migrations/versions/` altındaki **10 revizyon tamamen silinir**.
Modellerden tek initial revizyon üretilir.

`upgrade()` sırası:
1. `CREATE EXTENSION IF NOT EXISTS timescaledb` (idempotent; `db create`
   zaten kurmuş olur)
2. ENUM tipleri (SQLAlchemy metadata'dan üretir)
3. Tablolar + CHECK kısıtları (autogenerate, `naming_convention` ile —
   §2.6)
4. İndeksler (autogenerate) + `EXPRESSION_INDEXES` elle
5. `timescale_ddl()` — `create_hypertable(..., create_default_indexes => FALSE)`
   (§7.1, §7.6)
6. View'lar (§8)

`downgrade()` ters sırada. **`DROP EXTENSION timescaledb` YAPILMAZ:**
eklenti veritabanı düzeyindedir ve `db create`'in ürünüdür; düşürülürse
upgrade/downgrade döngüsü ikinci turda patlar.

`migrations/env.py`:
- `EXPRESSION_INDEXES` **korunur** — Alembic'in ifade tabanlı indeksleri
  geri okuyamaması PostgreSQL'de de geçerli.
- **v1'in `_timescaledb` filtresi KALDIRILDI.** Ölçüldü: `include_schemas=False`
  (varsayılan) ile Alembic yalnızca `search_path`'in ilk şemasına bakar;
  chunk'lı bir veritabanında autogenerate çıktısında tek bir
  `_timescaledb_*` nesnesi yok. Filtre gereksizdi ve asıl problemi (§7.1
  varsayılan indeks) gizliyordu. `include_schemas=False` korunur.
- `config.set_main_option("sqlalchemy.url", ...)` değişmez.

`script.py.mako` değişmez.

---

## 13. Yorum ve doküman politikası

Ölçüldü: `src/` + `tests/` + `migrations/` içinde MySQL'e özgü metin geçen
**92 dosya** var.

**(a) Gerekçesi MOTORA bağlı → silinir veya PostgreSQL karşılığıyla
yeniden yazılır.** `ERROR 1506`, `3780`, `3823`, `1493/1526`, `1064`,
`1213/1205`, `3140`, `sql_mode`, `CLIENT_FOUND_ROWS`,
`range_optimizer_max_mem_size`, `max_allowed_packet`, `InnoDB`, `utf8mb4`,
ENUM ordinal uyarısı, 3072 baytlık PK sınırı.

**(b) Gerekçesi VERİYE bağlı → aynen korunur.** *"ölçülen max 23 570
karakter (104 rapor, medyan 281)"*, *"AAPL'de 17 tarih"*, *"19 sembolün
19'unda da 404"*, *"ölçülen max 70 (JPM mutualfund)"*, DST/ofset notları,
monotonik kolon gerekçesi, `tradingPeriods`'ün tek doğruluk kaynağı
olması. Silinmesi bilgi kaybıdır.

**(c) Sınırda olanlar → gerekçe yeniden yazılır, karar korunur.**

**Karar kuralı (v1'de yoktu):** Bir yorumun **birden fazla** gerekçesi
varsa ve en az biri motordan bağımsızsa (c)'dir — motora bağlı cümle
silinir, karar ve kalan gerekçe korunur. Tüm gerekçeleri motora bağlıysa
(a)'dır. **Şüphede kalınırsa (c) seçilir:** bilgi kaybetmemek, fazladan
bir cümle bırakmaktan daha önemlidir.

Örnekler:
- `BarIntervalType` neden ENUM değil VARCHAR: MySQL gerekçesi (464 milyon
  satırda `ALTER TABLE` uzun sürer) PostgreSQL'de zayıflar, **ama karar
  korunur** — ikinci gerekçe (geçerli değer kümesinin `BAR_INTERVALS`'te
  tek kaynak olması) motordan bağımsızdır.
- `bar_interval` adının `interval` OLMAMASI: `INTERVAL` PostgreSQL'de de
  bir tip adıdır; gerekçe motorlar arası geçerli, yalnızca "MySQL'de
  rezerve kelime" ifadesi genelleştirilir.
- `holding_rank` (`RANK` pencere fonksiyonu) ve `market_summary.last_value`
  (`LAST_VALUE`): PostgreSQL'de de geçerli, karar korunur.
- `insider_transactions` hash yorumu: §2.5.3 — karar korunur, `ai_ci`
  ifadesi düzeltilir.
- `raw_json`'ın JSON tipi olmaması: §2.4, gerekçe aynen geçerli.

**Doküman politikası.** `docs/superpowers/specs/` altındaki 6 eski spec
silinmez: içerdikleri ölçümler hâlâ tek kanıt kaynağıdır. Bu doküman
onların motora ait kararlarını geçersiz kılar ve bunu başlığında beyan
eder.

---

## 14. Uygulama sırası

Her adım kendi doğrulamasıyla biter; bir adım yeşile dönmeden sonraki
başlamaz.

| # | Adım | Doğrulama |
|---|---|---|
| 0 | **Ön koşul:** `.env` §10 anahtarlarıyla güncellenir. Mevcut MySQL verisi kasıtlı terk edilir (§0), ama **MySQL DURDURULMAZ**: PostgreSQL 5432'de, MySQL 3306'da — port çakışması yok; ayrıca 3306'daki konteyner `local-dev-stack`'e ait ve beş başka proje aynı yığından besleniyor (uygulama başlangıcında ölçüldü). Durdurmanın migrasyona katkısı yok, maliyeti beş projenin kırılması | `.env` hazır, 5432 boş |
| 1 | `docker-compose.yml`, `.env.example`, `config.py`, `db.py` (§1, §5, §10) | Konteyner sağlıklı; `SELECT 1` ve `SELECT extversion FROM pg_extension WHERE extname='timescaledb'` çalışıyor |
| 2 | Tip katmanı: `models/base.py` (+`naming_convention`), `kinds.py` (collation + `row_cost` kaldırma), `columns.py` | `mypy --strict` temiz |
| 3 | 14 model dosyası + `snapshots.py`: `MYSQL_TABLE_ARGS`, unsigned→CHECK (35), ENUM (14), `TEXT`, `BYTEA`, `Identity`, `server_default` (14), §2.11. §2.5.4 taraması ve §2.11 ham-UPDATE taraması burada yapılır | `Base.metadata` 62 tablo; `mypy` temiz; üretilen DDL'de collation'sız `VARCHAR` yok |
| 4 | `models/views.py` (§8) | — |
| 5 | `persistence.py` → `PostgresRowWriter` + §4.1.1 dedupe + 16 dosyadaki referans | `pytest tests/unit/test_insert_chunk.py` yeşil |
| 6 | `runner.py` SQLSTATE (§6); `normalize.py` tz-aware; `.replace(tzinfo=None)` taraması (§2.3); `rescale.py` ham SQL (§9.4) | `pytest tests/unit` yeşil |
| 6b | §2.5 normalizasyonları: `datasets/symbols.py`, `cli.py:_filtered_symbols`, `scripts/seed_proxies.py` | `pytest tests/unit` yeşil |
| 6c | §3.2: `maintenance.py` silinir, `cli_bars.py` partition adımı + `SUM(...)` + öksüz satır sorgusu (§7.2) | `yfin bars maintain --help` çalışıyor |
| 7 | Alembic: `versions/` silinir, initial + `timescale_ddl()` + `env.py` (§12) | `yfin db create && yfin db upgrade head` temiz; **`yfin db revision` boş diff**; `downgrade base && upgrade head` hatasız |
| 8 | `conftest.py` / `helpers.py`: iki engine, şema izolasyonu (§9.1) | `pytest -m repo` toplanıyor, şema açılıyor ve düşüyor |
| 9a | Mekanik test uyarlamaları: §9.4 tablosu, backtick taraması | `pytest -m repo` yeşil |
| 9b | Yeni testler: §9.2 invaryant, §9.3 regresyonlar | `pytest -m repo` yeşil |
| 10a | §13(a) motora bağlı yorumlar — mekanik | — |
| 10b | §13(c) sınırda olanlar — yargı gerektirir | — |
| 11 | `pyproject.toml`, `__init__.py`, `cli.py` metinleri; `pip install -e .` (§11) | Son tarama (§15/6) boş |

Adım 7'deki **"boş diff"** koşulu kritiktir: modellerle migration'ın
ayrışmadığını kanıtlayan tek otomatik kontroldür (§9.1: testler Alembic
kullanmaz).

---

## 15. Bitiş ölçütleri

1. `ruff check` temiz.
2. `mypy --strict` temiz.
3. `pytest` (unit) yeşil.
4. `pytest -m repo` gerçek TimescaleDB'ye karşı yeşil.
5. `yfin db revision -m probe` **boş** migration üretiyor (dosya silinir).
6. Son tarama boş döner:
   ```
   grep -rniE 'mysql|innodb|utf8|charset|ai_ci|_bin|collate |pymysql|on_duplicate|get_lock|release_lock|is_used_lock|is_free_lock|longtext|mediumtext|tinyint|varbinary|sql_mode|auto_increment|max_allowed_packet|client_found_rows|last_insert_id|sha2\(|column_type|database\(\)|information_schema\.partitions' \
     --exclude-dir='*.egg-info' src tests migrations scripts pyproject.toml alembic.ini
   ```
   (`--exclude-dir` yerine adım 11'deki yeniden kurulum da yeterlidir;
   ikisi birden yapılır.)
7. `docker compose down -v && docker compose up -d && yfin db create && yfin db upgrade head`
   sıfırdan çalışıyor.
8. `yfin db upgrade head && yfin db downgrade base && yfin db upgrade head`
   hatasız koşar (§12).
9. `timescaledb_information.chunks` her iki hypertable için >0 ve <100
   chunk gösteriyor (§7.1 kabul ölçütü).

`pytest -m live` bitiş ölçütü **değildir**: ağ ve Yahoo tarafındaki
değişkenlere bağlıdır. Kullanıcı isterse ayrıca koşulur.

---

## 16. Bilinen riskler

v1'in R1 (GREATEST boolean), R2 (`by_range` DATE), R3'ün doğruluk kısmı
(hypertable FK) ve R5 (`_timescaledb` autogenerate gürültüsü) **ölçümle
kapandı** ve listeden çıkarıldı.

| # | Risk | Etki | Plan |
|---|---|---|---|
| R1 | Hypertable → `symbols` FK'sinin yazma maliyeti | `price_bars` insert hızı | §7.2: gözlenir; kabul edilemezse FK geri alınır ve doküman güncellenir |
| R2 | `search_path`'te `public` unutulursa `create_hypertable` çözülemez | Testler sessizce düz tabloya düşer | §9.1'de açık; §9.4 hypertable testi yakalar |
| R3 | §2.5.4 taramasında yeni bir `ai_ci` bağımlılığı çıkabilir | Sessiz satır çoğalması | §2.5.4 karar kuralı; bulgu adım 3'te çözülür |
| R4 | §4.1.1 dedupe'u atlanan bir yol kalırsa | `21000 cardinality_violation` üretimde | §9.3 testi + `write()` tek giriş noktasıdır |
| R5 | `naming_convention` mevcut açık indeks adlarıyla çakışabilir | Migration'da yinelenen ad | Adım 2'de `Base.metadata` üzerinden ad listesi çıkarılıp çakışma kontrol edilir |
| R6 | §2.11 `onupdate` ORM dışı ham UPDATE'lerde tazelenmez | `proxies.updated_at` bayat kalır | Adım 3'te `proxies`'e ham UPDATE yazan yer taranır; varsa açıkça `updated_at` set edilir |
| R7 | `.env` hâlâ MySQL değerleri taşıyor (`.gitignore`'da) | Yerel koşu başarısız | Adım 0'ın ön koşulu |

---

## Ek A: Ölçüm komutları

```
docker pull timescale/timescaledb:latest-pg18
docker run --rm timescale/timescaledb:latest-pg18 postgres --version
  -> postgres (PostgreSQL) 18.6
psql -c "select extversion from pg_extension where extname='timescaledb'"
  -> 2.29.2
lsof -nP -iTCP:5432 -sTCP:LISTEN   -> bos
```

Kod tabanı ölçümleri (`Base.metadata` + `grep` + AST):
```
len(Base.metadata.tables)                              -> 62
grep -rl MySQLRowWriter src tests | wc -l              -> 16
grep -rl 'now(UTC).replace(tzinfo=None)' src tests     -> 12 dosya, 17 satir
grep -rn 'replace(tzinfo=None)' ... (now disi)         -> 24 satir
grep -rl 'tzinfo is None' tests                        -> 4 dosya
grep -rl __tablename__ src/yfin/models                 -> 14 dosya
Enum kolonlari                                          -> 14 tip, 16 kolon
Boolean + server_default                                -> 14 kolon
type.unsigned == True                                   -> 35 kolon
```

`key_columns` ↔ PK/UNIQUE: `Base.metadata`'dan PK ve UNIQUE kümeleri
çıkarılıp `src/yfin/datasets/**.py` içindeki `TableWrite(...)` çağrıları
AST ile tarandı. Statik olarak çözülebilen 24 çağrının 24'ü eşleşti. İlk
taramada tek şüpheli kalıp `financials/statements.py:182`'deki
`(*GATE_KEY, "item_key")` yıldız-açılımıydı; `GATE_KEY` elle çözüldüğünde
`financial_facts` PK'sına eşit olduğu görüldü. **Nihai sonuç 24/24'tür.**
Yıldız-açılımlı ve değişkenden gelen çağrılar statik olarak çözülemediği
için §9.2'deki çalışma zamanı invaryant testi zorunludur.

PostgreSQL/TimescaleDB davranış ölçümleri (`timescale/timescaledb:latest-pg18`
konteynerinde, PG 18.6 + TSDB 2.29.2, psql ile) — §2.3, §2.5, §2.6, §2.8,
§2.9, §2.10, §3.1, §3.3, §3.4, §3.5, §4.1, §4.2, §5.1, §5.3, §7.1, §7.2,
§8, §9.1, §10 bölümlerinde "(ölçüldü)" ibaresiyle işaretlenmiştir.

---

## Ek B: v1 → v2 değişiklik özeti

Üç bağımsız reviewer (teknik doğrulama / kod sadakati / iç tutarlılık)
aşağıdakileri buldu. Kritik olanlar:

**Yanlış olduğu için silinen veya tersine çevrilen kararlar**
1. §2.5.2 (v1): `insider_transactions.action` kolonu üzerinden
   normalizasyon. **Öyle bir kolon yok**; mevcut davranış bilinçli olarak
   case-sensitive. Öneri uygulansaydı doğru çalışan bir ayrımı bozardı.
   → §2.5.3 olarak "bağımlılık yok" bulgusuna çevrildi, §9.3'teki test
   kaldırıldı.
2. §2.3 (v1): `sqlalchemy.TIMESTAMP(timezone=True, precision=6)` — generic
   tip `precision` **kabul etmez**, kod import anında patlardı.
3. §5.3 (v1): `pg_locks` bit ayrıştırması — negatif anahtarda yanlış
   sonuç, büyük anahtarda `22003` hatası. Teşhis sorgusu hata yolunun
   içinde patlayacaktı.
4. §2.9 (v1): "adsız `Enum()` hata verir" ve "iki `Enum()` nesnesi
   migration'ı patlatır" — ikisi de yanlış.
5. §12 (v1): `_timescaledb` autogenerate filtresi — gereksizdi ve asıl
   problemi (varsayılan hypertable indeksi) gizliyordu.

**Atlandığı için eklenen konular**
6. `maintenance.py` modülünün tamamı (§3.2).
7. `proxies.updated_at`'te `ON UPDATE CURRENT_TIMESTAMP` (§2.11).
8. `ON CONFLICT DO UPDATE` dilim içi anahtar tekrarı (§4.1.1) — üretimde
   patlayacak en olası yer.
9. Advisory kilitlerinin veritabanı kapsamlı olması (§5.2.1).
10. `kinds.py` `String(n)` kolonlarının collation'sız kalması (§2.5).
11. `Base.metadata` `naming_convention` yokluğu — "boş diff" kapısını
    açılamaz kılıyordu (§2.6).
12. `create_hypertable`'ın varsayılan DESC indeksi — aynı kapıyı ikinci
    kez kapatıyordu (§7.1).
13. `bootstrap_url()` ile `information_schema.schemata`'nın veritabanı
    kapsamı çelişkisi (§9.1).
14. `connect_args["options"]`'ın tek dize olması (§5.4).
15. `egg-info` yüzünden §15/6 taramasının asla boşalmaması (§11).
16. `downgrade` doğrulaması ve `DROP EXTENSION` tuzağı (§12, §15/8).
17. Testlerdeki `LAST_INSERT_ID`, `SHA2()`, `COLUMN_TYPE`, `DATABASE()`,
    `SUM(x IS NULL)`, backtick'ler (§9.4).
18. §14'te §2.5 normalizasyon dosyalarının hiç geçmemesi (adım 6b).

**Düzeltilen sayılar**
`MySQLRowWriter` 6 → **16 dosya** · tz deseni 7 → **12 dosya/17 satır**
(+24 satır diğer) · ENUM "altı"/12 → **14 tip, 16 kolon** · Boolean
`server_default` 4 → **14 kolon** · unsigned kolon (verilmemiş) → **35** ·
model dosyası 15 → **14 + snapshots.py** · TimescaleDB 2.26.3 → **2.29.2**
· `INTERVAL '1 year'` → 360 gün olduğu için **`'365 days'`**.
