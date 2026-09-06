# yfinance → MySQL Veri Hattı — Tasarım Dokümanı

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
- **Durum:** Uygulandı ve çalışan sistemde doğrulandı (bkz. Ek B)
- **Proje kökü:** `~/Projects/learn/yfinance/`
- **Kapsam:** yfinance `Ticker` API'sinin "Ticker and Tickers" + "Stock" bölümü

> **BU DOKÜMAN SONRAKİ SPEC'LERLE GENİŞLETİLMİŞTİR.** Aşağıdaki bölümler
> için bağlayıcı olan metin artık başka dokümanlardadır:
> §5.2 `price_history` → **P §3.7** (`is_repaired`, `monotonic_columns`);
> §6.1 "boş liste silmez" → `persistence.py`'nin `replace_scope` invariant'ı
> ile birlikte okunmalıdır; §6.3 `dividends`/`splits`/`capital_gains`
> "bağımsız çağrı" kararı → **P §6.4 ile GERİ ALINDI** (paylaşılan onarılmış
> çerçeve); §7.1 tek-process `ThreadPoolExecutor` → **P §4** çok-process
> shard mimarisi; §7.5 process-global rate limit → **P §7.2** shard başına;
> §8.1 statü/çıkış kodları → **P §8.1** (`not_attempted`, `EXIT_NO_PROXY=5`).

---

## 1. Amaç

yfinance kütüphanesinin döndürdüğü hisse senedi verisini **eksiksiz ve tip
güvenli** biçimde MySQL 8'e yazan, tekrar çalıştırılabilir (idempotent) bir
veri hattı kurmak. İlişkiler sembol kodu üzerinden kurulur. Mimari, ileride
eklenecek diğer yfinance API'lerini (financials, options, holders, earnings)
mevcut koda dokunmadan kabul edecek şekilde tasarlanır.

### Kapsam içi

**11 kullanıcı-görünür veri kümesi:** `isin`, `history`, `history_metadata`,
`dividends`, `splits`, `actions`, `capital_gains`, `shares_full`, `info`,
`fast_info`, `news`.

Bunlara ek olarak **1 altyapı dataset'i** (`symbols`) vardır; kullanıcı
seçmez, her çalıştırmada zorunlu olarak ilk çalışır. `actions` registry'de
kayıt değil, bir alias'tır (bkz. §6.3). Dolayısıyla registry'de 11 kayıt
bulunur: 10 kullanıcı-görünür + `symbols`.

### Kapsam dışı (bilinçli, YAGNI)

REST API, dahili scheduler, web arayüzü, sembol evreni keşfi (semboller
elle eklenir).

**Bu listenin sonradan sahiplenilen kalemleri:** `financials` → F,
`holders` → AH, `earnings` → F §1 (kaynak koşulsuz `None` döndürdüğü için
gerekçeli olarak elendi), `intraday veri` → PH (uygulanmayı bekliyor),
`Sector`/`Industry` → `2026-09-04-sector-industry-design.md` (**SI**,
taslak onaylandı, uygulanmayı bekliyor).

**Hâlâ sahiplenilmemiş yfinance API'leri** (hiçbir spec'in kapsamında
değil; gerekçe yazılana kadar açık boşluktur):

| API | Not |
|---|---|
| `Ticker.option_chain` / `options` | Bu listede "options" olarak dışlanmıştı; referans dokümantasyonda da bölümü yok |
| `Search`, `Lookup` | Sembol evreni keşfini otomatikleştirir; "semboller elle eklenir" kararının bilinçli olarak yeniden tartılması gerekir |
| `screen` / `EquityQuery` / `FundQuery` / `ETFQuery` | F §6.5 yalnızca `market_summary`'nin doğurduğu `screen()` isteğini tartışıyor; screener'ın kendisi hiç değerlendirilmedi |
| `WebSocket` / `AsyncWebSocket` | Canlı akış; batch ETL mimarisiyle yapısal olarak uyumsuz — açık bir "hayır" kararı yazılmalı |
| `Auth` | Kimlikli uçlar; P yalnızca `YfData` singleton'ını inceledi |

(`Tickers` ve `download` gerekçeli olarak elenmiştir: §7.4 ve PH §1.)

---

## 2. Kararlar ve gerekçeleri

| Karar | Seçim | Gerekçe |
|---|---|---|
| Sembol evreni | Çok pazarlı (hisse, ETF, fon, kripto, FX, vadeli, endeks) | §4.3'te 6 farklı pazar tipinde 11 dataset denendi, hiçbiri exception fırlatmadı |
| History kapsamı | `interval="1d"`, `period="max"` | Haftalık/aylık günlükten türetilebilir; intraday 60 günle sınırlı |
| Çalışma modeli | CLI + idempotent upsert | Cron'a bağlamak tek satır; scheduler bağımlılığı gereksiz |
| `info` saklama | Hibrit: tipli kolonlar + `raw_json` | Sorgu performansı ve "hiçbir alan kaybolmaz" garantisi birlikte |
| Snapshot geçmişi | Son durum + ayrı `_history` tablosu | Trend analizi mümkün; `content_hash` ile tekrar yazma engellenir |
| DB katmanı | SQLAlchemy 2.0 + Alembic | Tipli modeller tek doğruluk kaynağı; şema evrimi versiyonlu |
| Mimari | Dataset-Plugin (Registry) | Yeni API eklemek = tek yeni dosya |
| Test | Fixture tabanlı + entegrasyon | Deterministik ve hızlı; canlı test ayrı işaretle |
| Sembol silme | Soft delete (`is_active=0`), FK `ON DELETE RESTRICT` | Tek `DELETE` 40 yıllık geçmişi geri dönüşsüz siler (§4.2 K/O4) |
| Toplu `history` çekimi | **Kullanılmıyor** | Sembol başına watermark, hata izolasyonu ve tek-transaction ilkeleriyle uyuşmuyor (§7.4) |

---

## 3. Ortam

- **MySQL:** Docker container `mysql`, **8.3.0**, port `3306`, `root`/`root`.
  Şema `yfinance`, test şeması `yfinance_test`.
  `sql_mode=STRICT_TRANS_TABLES`, `innodb_default_row_format=dynamic`,
  `innodb_page_size=16384`.
  **Host `localhost`'tur, `127.0.0.1` değil.** Bu makinede `127.0.0.1:3306`'ı
  ayrı bir yerel MySQL (**9.3.0**) dinliyor; hedef container yalnızca `::1`
  üzerinden erişilebiliyor ve `localhost` oraya gidiyor. `127.0.0.1` ile
  bağlanmak `Access denied for user 'root'@'localhost'` verir — bu bir şifre
  hatası değil, **yanlış sunucudur**. Doğrulama: `SELECT VERSION()` → `8.3.0`.
- **Python:** 3.13.5
- **yfinance:** 1.7.0 (pandas 3.0.5, numpy 2.5.2, curl_cffi 0.16.3)

---

## 4. API keşif bulguları

Tümü canlı Yahoo çağrılarıyla (2026-09-04) doğrulandı.

### 4.1 Doğrulanmış davranışlar

| Bulgu | Gözlem | Tasarıma etkisi |
|---|---|---|
| Sentinel ISIN | `'-'` → THYAO.IS, BTC-USD, ^GSPC, GC=F, EURUSD=X | `'-'`, `''`, `'N/A'` → `NULL` |
| tz-aware index | AAPL `America/New_York`, THYAO `Europe/Istanbul` | `session_date` (yerel) + `ts_utc` ayrımı |
| `history` kolonları | `auto_adjust=False` → `Adj Close`; parametre kombinasyonu **uyarı üretmiyor** | `adj_close` ayrı kolon |
| `capital_gains` | Hisselerde boş `Series`, `dtype=object` | `empty` ≠ `failed` |
| `info` alan sayısı | AAPL 187, THYAO 161, MSFT 182, SPY 103, BTC-USD 91, VFIAX 86 | Alan seti değişken → `raw_json` zorunlu |
| `info` nested anahtarlar | **Tam olarak 3**: `companyOfficers`, `corporateActions`, `executiveTeam`. Son ikisi 6/6 sembolde boş liste | İlki tabloya, kalanı `raw_json`'da |
| `fast_info` | Kaynakta hardcoded 20 anahtar (`quote.py:_public_keys`), 5 pazarda birebir aynı | Tam tipli kolon seti mümkün |
| `actions` | `dividends` ∪ `splits` (AAPL: 92+5=97, birebir) | Tablo değil, **VIEW** |
| Küçük fiyatlar | AAPL 1980 → `0.128348`; THYAO 2000 → `0.001870` | `DECIMAL(28,12)`; round-trip kaybı yok (DB'de doğrulandı) |
| `news` sembol ilişkisi | `content.finance.stockTickers` → 50/50 haberde mevcut; 27/50 çok sembollü | M:N gerçek; ama bkz. §5.5 |
| `get_news(count, tab)` | `tab ∈ {"news","all","press releases"}`; `count` üst sınır (AAPL 50, THYAO 32) | `count=50, tab="all"` |
| `DECIMAL(28,12)` / `(38,0)` | MySQL 8.3'te geçerli; taşma strict mode'da **hata**, sessiz kırpma değil | Büyük değerler ayrı tiple |
| `v_actions` VIEW | Oluşuyor, doğru sonuç, `value decimal(28,12)` | Tasarım doğrulandı |

### 4.2 Düzeltilen hatalı varsayımlar

Aşağıdakiler ilk taslakta yanlıştı; her biri canlı deneyle çürütüldü.

| Yanlış varsayım | Gerçek | Kanıt |
|---|---|---|
| `get_shares_full(start=None)` tüm geçmişi verir | **Son 548 gün ile sınırlar** (`base.py:511`) | `start=None` → 67 satır; `start="1990-01-01"` → 420 satır |
| `shares_full` tarihleri benzersiz | Aynı tarihte farklı değerler var | AAPL: 67 satır / 49 benzersiz tarih; 17 tarihte değerler farklı |
| `get_shares_full` her zaman `Series` döner | **`None` de dönebilir** | SPY, BTC-USD, EURUSD=X, GC=F, ^GSPC → `None` |
| `history_metadata` bir `dict` | `HistoryMetadata`, bir `Mapping`; `tradingPeriods` bir **DataFrame** | `isinstance(m, dict)` → `False`; `json.dumps(m)` → `TypeError` |
| `Decimal(repr(x))` güvenli | numpy 2.x'te `repr(np.float64(0.00187))` = `'np.float64(0.00187)'` | `Decimal(repr(...))` → `InvalidOperation` |
| Epoch alanı 3 tane | **21 tane** | §8.4 tam liste |
| UTC'ye çevirince ABD seansı geri kayar | Tersi: **pozitif ofsetli borsalar** (BIST, Tokyo) geri kayar | AAPL `00:00-04:00`→UTC aynı gün; THYAO `00:00+03:00`→UTC bir gün geri |
| Fonlarda `capital_gains` dolu gelir | 7 fon/ETF'in **hiçbirinde** dolu değil | VFIAX, FCNTX, VTSAX, SWPPX, FMAGX, PRNHX, VWELX, SPY → hepsi `len 0` |
| `utf8mb4_0900_ai_ci` sembol PK'sı için uygun | Case-insensitive: `'AAPL'` = `'aapl'` | `Duplicate entry 'aapl' for key 'symbols.PRIMARY'` |
| `raw_json` `JSON` tipiyle kayıpsız | Anahtar sırasını bozar, `NaN`'ı reddeder, float'ı kırpar | `0.001870` → `0.00187`; `{"x":NaN}` → `ERROR 3140` |
| `HistoryMetadata` Mapping sözleşmesine uyar | **Uymuyor:** `keys()` `tradingPeriods`'ı listeler, `__getitem__` aynı anahtarda `KeyError` fırlatır (`scrapers/history.py:55`) | VFIAX'te `dict(m)` → `KeyError: 'tradingPeriods'`; sembolün 11 dataset'i birden düşerdi |
| `history_metadata.firstTradeDate` epoch sayıdır | yfinance onu **zaten tz-aware `Timestamp`'e çevirmiş** döndürür | `type(m['firstTradeDate'])` → `Timestamp`; epoch olarak yorumlamak `NULL` üretiyordu |
| `get_shares_full` fon/ETF'te hep `None` | `start` verildiğinde **veri dönüyor** | SPY `start=None` → `None`; `start="1970-01-01"` → 176 satır |

### 4.3 Çok pazarlılık matrisi

11 dataset × 6 sembol; **hiçbiri exception fırlatmadı**.

| dataset | SPY (ETF) | BTC-USD | THYAO.IS | EURUSD=X | GC=F | ^GSPC |
|---|---|---|---|---|---|---|
| isin | `US78462F1030` | `'-'` | `'-'` | `'-'` | `'-'` | `'-'` |
| history | 8457 | 4371 | 6768 | 5906 | 6528 | 24786 |
| history_metadata | 31 | 31 | 31 | **32** | **30** | 31 |
| dividends | 135 | 0 | 4 | 0 | 0 | 0 |
| splits | 0 | 0 | 4 | 0 | 0 | 0 |
| capital_gains | 0 | 0 | 0 | 0 | 0 | 0 |
| shares_full | 176 | **None** | 354 | **None** | **None** | **None** |
| info | 103 | 91 | 161 | 70 | 75 | 70 |
| fast_info | 20 | 20 | 20 | 20 | 20 | 20 |
| news | 50 | 50 | 32 | 50 | 50 | 50 |

Ek gözlemler:
- `history_metadata` anahtar sayısı sembole göre **değişken** (30–32) → `raw_json` zorunlu.
- `history` kolon seti değişken: fon/ETF'te 9. kolon olarak `Capital Gains` eklenir.
- `fast_info.marketCap` ve `.shares` ETF/kripto/FX/endekste `None`.
- `shares_full` satırı **`start="1970-01-01"` ile** ölçülmüştür; `start=None`
  ile SPY de `None` döner (§4.2). Tabloda `None` yazan dört sembol `start`
  verildiğinde de veri döndürmez.

**Yatırım fonu (7. pazar tipi).** Matris ilk yazıldığında altı pazar tipi
kapsıyordu; `VFIAX` ile yapılan çalıştırma `HistoryMetadata`'nın Mapping
ihlalini ortaya çıkardı (§4.2) ve sembolün tamamını `unknown_symbol` yapıyordu.
Düzeltme sonrası VFIAX değerleri: `history` 6489, `dividends` 99, `info` 86,
`fast_info` 20, `shares_full` 0, `splits`/`capital_gains` 0, `isin` `'-'`.
Fon tipi artık düzenli kapsamdadır.

---

## 5. Veri modeli

Motor InnoDB, varsayılan charset `utf8mb4` / `utf8mb4_0900_ai_ci`.

### 5.1 Collation istisnaları (zorunlu)

`utf8mb4_0900_ai_ci` case-insensitive olduğu için sembol anahtarı için
**kullanılamaz**. Ayrıca FK kolonlarının collation'ı ebeveynle birebir
eşleşmelidir (`ERROR 3780` aksi hâlde).

| Kolon | Tip ve collation | Gerekçe |
|---|---|---|
| `symbols.symbol` ve **tüm** sembol FK kolonları | `VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin` | Yahoo sembolleri ASCII (`AAPL`, `THYAO.IS`, `BRK-B`, `BTC-USD`, `^GSPC`, `EURUSD=X`); büyük/küçük harf ayrımı korunur; index 128→32 byte |
| `news.news_id`, `news_symbols.news_id` | `VARCHAR(36) CHARACTER SET ascii COLLATE ascii_bin` | UUID, sabit 36 karakter (ölçüldü) |
| `*.content_hash` | `VARCHAR(64) CHARACTER SET ascii` | SHA-256 hex; 256→64 byte |
| `company_officers.name` | `VARCHAR(255) COLLATE utf8mb4_0900_as_cs` | `'Tim Cook'` ≠ `'TIM COOK'` olmalı |

### 5.2 Tablolar

Nullability her tablo için açıkça belirtilmiştir; §9.3 canlı testi bu listeyi
doğrular.

**`symbols`** — PK `symbol`
`isin` NULL, `quote_type` NULL, `exchange` NULL, `full_exchange_name` NULL,
`currency` NULL, `timezone` NULL, `short_name` NULL, `long_name` NULL,
`first_trade_date` NULL, `is_active` BOOL NOT NULL DEFAULT 1,
`unknown_streak` INT NOT NULL DEFAULT 0, `last_seen_at` DATETIME(6) NULL,
`created_at`/`updated_at` DATETIME(6) NOT NULL.
`isin` **UNIQUE değildir** — aynı ISIN farklı borsalarda listelenebilir.
`unknown_streak` §8.8'in delist sayacıdır; `last_seen_at` en son başarılı
çözümlemenin damgasıdır.
`is_active` ve `isin` kolonları `symbols` dataset'inin upsert kapsamının
**dışındadır** (§6.1) — ilki kullanıcı kararını, ikincisi `isin` dataset'inin
sahipliğini korur.

**`price_history`** — PK (`symbol`, `session_date`)
`ts_utc` DATETIME(6) NOT NULL, `open`/`high`/`low`/`close`/`adj_close`
DECIMAL(28,12) (`close` NOT NULL, diğerleri NULL),
`volume` BIGINT UNSIGNED NULL,
`dividend`/`split_ratio`/`capital_gain` DECIMAL(28,12) NOT NULL DEFAULT 0.
Son üç kolon **türev bilgidir, otorite değildir**; tekil doğruluk kaynağı
`dividends`/`splits`/`capital_gains` tablolarıdır ve `v_actions` yalnızca
onları okur.

**`history_metadata`** — PK `symbol`
Tipli alanlar: `currency`, `exchange_name`, `full_exchange_name`,
`instrument_type`, `timezone`, `gmt_offset`, `first_trade_date`,
`regular_market_time`, `regular_market_price`, `fifty_two_week_high`,
`fifty_two_week_low`, `price_hint`, `data_granularity`, `range`,
`has_pre_post_market_data`, `short_name`, `long_name`, `chart_previous_close`,
`regular_market_day_high`, `regular_market_day_low`, `regular_market_volume`
(hepsi NULL kabul eder) + `raw_json` LONGTEXT NOT NULL.
Kaynakta 30–32 anahtar var; `'YF repair?'` gibi boşluk/soru işareti içeren
anahtarlar yalnızca `raw_json`'da tutulur.

**`dividends`** — PK (`symbol`, `ex_date`) · `amount` DECIMAL(28,12) NOT NULL
**`splits`** — PK (`symbol`, `split_date`) · `ratio` DECIMAL(28,12) NOT NULL
**`capital_gains`** — PK (`symbol`, `gain_date`) · `amount` DECIMAL(28,12) NOT NULL

**`shares_full`** — PK (`symbol`, `as_of_date`, `shares`)
`shares` BIGINT UNSIGNED NOT NULL, `ts_utc` DATETIME(6) NULL (kaynak
index'inin ham UTC karşılığı; `as_of_date` yerel tarihtir).
`shares` PK'ya **dahildir**: kaynakta aynı tarihte farklı değerler geliyor
(AAPL'de 17 tarih). `(symbol, as_of_date)` ikilisi olsaydı hangi değerin
kazanacağı çalıştırma sırasına bağlı olurdu.

**`ticker_info`** — PK `symbol` · **`ticker_info_history`** — PK (`symbol`, `fetched_at`)
Aynı kolon seti: **187 tipli kolon** + `raw_json` LONGTEXT NOT NULL +
`content_hash` NOT NULL + `fetched_at` DATETIME(6) NOT NULL.
Kolon seti dört referans sembolün (§9.1) alan birleşiminden seçildi; kimlik,
sınıflandırma, fiyat, hacim, değerleme, oran, temettü, analist hedefi, fon,
kripto ve 21 epoch alanını kapsar. Ölçülen satır maliyeti **~11 900 byte** —
65 535 bütçesinin çok altında (§5.4). Kolon adı, kaynak anahtarı ve tip tek
bir listede durur (`models/fields.py`); tipli kolon ile dönüştürücü aynı
tanımdan üretilir (§6.1).

**`ticker_fast_info`** — PK `symbol` · **`ticker_fast_info_history`** — PK (`symbol`, `fetched_at`)
20 tipli kolon (**hepsi NULL kabul eder** — `marketCap`/`shares` ETF, kripto,
FX ve endekslerde `None`) + `raw_json` LONGTEXT NOT NULL + `content_hash` +
`fetched_at`. `raw_json` burada da tutulur: `content_hash` kanonik JSON
üzerinden hesaplandığı için hash'in doğrulanabilmesi gövdeyi gerektirir.

**`company_officers`** — PK (`symbol`, `name`)
`title` NULL, `age` NULL, `year_born` NULL, `fiscal_year` NULL,
`total_pay` NULL, `exercised_value` NULL, `unexercised_value` NULL.
`age`, `year_born`, `total_pay` kaynakta opsiyoneldir (10 kayıttan sırasıyla
8, 8, 5'inde var). Surrogate `id` **yoktur** — doğal anahtar yeterlidir ve
her upsert'te AUTO_INCREMENT yakılmasını önler.

**`news`** — PK `news_id`
`title` NOT NULL, `summary` TEXT NULL, `description` TEXT NULL,
`content_type` NULL, `pub_date` DATETIME(6) NOT NULL,
`display_time` DATETIME(6) NULL, `provider_name` NULL, `provider_url` NULL,
`provider_source_id` NULL, `canonical_url` TEXT NULL,
`click_through_url` TEXT NULL, `thumbnail_url` TEXT NULL,
`thumbnail_width` NULL, `thumbnail_height` NULL,
`raw_json` LONGTEXT NOT NULL.
`thumbnail_*` kolonlarına **`tag="original"` çözünürlüğü** yazılır; diğer
çözünürlükler `raw_json`'da kalır. `thumbnail` `None` olabilir (50 haberin
5'inde). `news` tablosu `symbols`'a FK ile bağlı **değildir**.

**`news_symbols`** — PK (`news_id`, `symbol`)
`news_id` → `news(news_id)` FK, `ON DELETE CASCADE`.
`symbol` üzerinde **FK yoktur** (bkz. §5.5).
`is_known` BOOL NOT NULL — sembol `symbols` tablosunda var mı.

**`sync_runs`** — PK `id` BIGINT UNSIGNED AUTO
`started_at` DATETIME(6) NOT NULL, `finished_at` DATETIME(6) NULL,
`status` ENUM('running','ok','partial','failed') NOT NULL,
`symbol_count`, `dataset_count`, `rows_fetched`, `rows_written`,
`rows_verified`, `rows_skipped`.

**`sync_run_items`** — PK `id` BIGINT UNSIGNED AUTO
`run_id` → `sync_runs(id)` FK `ON DELETE CASCADE`,
`symbol` VARCHAR(32) ascii_bin NOT NULL (**FK yok**, bkz. §5.5),
`dataset` NOT NULL,
`status` ENUM('ok','empty','skipped','failed','unknown_symbol') NOT NULL,
`table_name` NULL, `rows_fetched`, `rows_written`, `rows_verified`,
`duration_ms`, `error` TEXT NULL.
Çok tabloya yazan dataset'ler için tablo başına bir satır yazılır.

### 5.3 View

```sql
CREATE SQL SECURITY INVOKER VIEW v_actions AS
  SELECT symbol, ex_date    AS action_date,
         CAST('DIVIDEND'     AS CHAR(16)) AS action_type, amount AS action_value FROM dividends
  UNION ALL
  SELECT symbol, split_date, CAST('SPLIT'        AS CHAR(16)), ratio  FROM splits
  UNION ALL
  SELECT symbol, gain_date,  CAST('CAPITAL_GAIN' AS CHAR(16)), amount FROM capital_gains;
```

`CAST(... AS CHAR(16))`: literal uzunluğu view'ın kolon tipini belirler;
sabitlemezsek yeni bir action türü eklendiğinde tip sessizce değişir.
`action_value` adı `value`'dan yeğlenir (ORM/dialect taşınabilirliği).
`SQL SECURITY INVOKER` varsayılan `DEFINER`'dan daha doğru bir varsayılandır.

### 5.4 Tip kararları

- **Fiyat ve oranlar `DECIMAL(28,12)`.** DB'de round-trip kaybı olmadığı
  doğrulandı (`0.128348000000`, `0.001870000000`). Tamsayı kapasitesi 10¹⁶.
- **Büyük değerler ayrı tiple.** `marketCap`, `totalRevenue`,
  `enterpriseValue` → `DECIMAL(38,0)` / `BIGINT UNSIGNED`. "Tüm sayısal info
  alanları DECIMAL(28,12)" kestirmesi `ERROR 1264` verir.
- **Tüm zaman damgaları `DATETIME(6)`.** `DATETIME(0)` aynı saniyede PK
  çakışması üretir (`ERROR 1062`) **ve kesirleri yuvarlar** (`10:00:00.75` →
  `10:00:01`). `fetched_at` DB fonksiyonuyla değil, **Python tarafında sembol
  başına bir kez** üretilir — §8.7'deki tek-transaction sınırıyla tutarlı olur.
- **`session_date` DATE + `ts_utc` DATETIME(6) birlikte.** Anahtar borsanın
  yerel seans tarihidir. Pozitif UTC ofsetli borsalarda (BIST, Tokyo) UTC'ye
  çevirmek tarihi bir gün geri kaydırır: THYAO `2000-05-10 00:00+03:00` →
  `2000-05-09 21:00 UTC`. `ts_utc` ham gerçeği korur.
- **`raw_json` `LONGTEXT`, `JSON` değil.** `JSON` tipi anahtar sırasını
  değiştirir (hash yeniden hesaplanamaz), `NaN` içeren gövdeyi reddeder
  (`ERROR 3140` → tüm sembol transaction'ı rollback) ve float'ı DOUBLE'a
  düşürür (`0.001870` → `0.00187`). `LONGTEXT` byte-for-byte sadıktır;
  `JSON_EXTRACT` üzerinde yine çalışır.
- **Satır boyutu bütçesi 65535 byte.** utf8mb4 `VARCHAR(255)` = 1020 byte;
  yalnızca `VARCHAR(255)` kullanılsa ~63 kolonda limit dolar (`ERROR 1118` ile
  doğrulandı). Kural: metin alanları gerçek uzunluğa göre `VARCHAR(16…128)`,
  uzun serbest metinler (`longBusinessSummary`, `address1`, `website`,
  `irWebsite`) `TEXT` (satırdan yalnızca ~20 byte götürür). Bu kuralla 187
  kolonlu `ticker_info` **~11 900 byte** tutar. §8.5'teki kolon terfisi bu
  bütçe hesaplanmadan yapılmaz; hesap `models/columns.estimated_row_size()`
  ile yapılır ve bir testle bağlanmıştır (§9.1).

### 5.5 FK kapsamı — istisnalar açıkça tanımlı

Sembol FK'sı **yalnızca sembol-kapsamlı veri tablolarında** vardır:
`price_history`, `history_metadata`, `dividends`, `splits`, `capital_gains`,
`shares_full`, `ticker_info(_history)`, `ticker_fast_info(_history)`,
`company_officers`. Bunlarda `ON UPDATE CASCADE ON DELETE RESTRICT`.

Üç tablo bilinçli olarak FK taşımaz:

| Tablo | Gerekçe |
|---|---|
| `sync_run_items.symbol` | Çözülemeyen sembol için `unknown_symbol` kaydı yazılamazdı (`ERROR 1452`). Denetim kaydı, sembol silinse de kalmalıdır. |
| `news_symbols.symbol` | Kaynakta evren dışı semboller geliyor (bir AAPL haberinde `005930.KS`, `^GSPC`, `IRTC`). FK olsaydı sembol başına tek transaction gereği **tüm sembolün verisi rollback olurdu**. Ham etiket olarak saklanır; `is_known` bayrağı `symbols`'ta olup olmadığını işaretler. |
| `news` | Sembolden bağımsız varlık; bağlantı `news_symbols` üzerindendir. |

**Öksüz haber temizliği.** `news` FK ile temizlenemez. `news_symbols`'tan son
bağlantısı kalkan haber `news` tablosunda öksüz kalır ve `raw_json` taşıdığı
için bu şişme ucuz değildir. `yfin prune` komutu bunları siler:
`DELETE FROM news WHERE news_id NOT IN (SELECT news_id FROM news_symbols)`.

**Sembol silme politikası.** `ON DELETE RESTRICT`, `is_active=0` soft-delete
politikasını DB seviyesinde zorlar. Gerçek silme yalnızca
`yfin symbols purge --force` ile yapılır ve bu komut ilgili satırları
açıkça, sırayla siler. Aksi hâlde tek bir `DELETE` 40 yıllık geçmişi geri
dönüşsüz siler ve yeniden çekmek dakikalarca Yahoo trafiği demektir.

### 5.6 İndeksler

- `price_history`: `INDEX (session_date)`
- `dividends (ex_date)`, `splits (split_date)`, `capital_gains (gain_date)` —
  `v_actions` üzerinden tarih aralığı sorgusu aksi hâlde full scan yapar
  (`EXPLAIN` ile doğrulandı)
- `shares_full (as_of_date)`
- `ticker_info_history (symbol, fetched_at DESC)`, aynısı `ticker_fast_info_history`
- `news (pub_date)`
- `symbols (exchange)`, `symbols (isin)`
- `sync_runs (started_at DESC)`, `sync_run_items (run_id, status)`,
  `sync_run_items (symbol, dataset)`
- `news_symbols (symbol)` — InnoDB FK'sız olduğu için **açıkça tanımlanır**

---

## 6. Mimari

### 6.1 Dataset sözleşmesi

Sözleşme modülü (`datasets/base.py`) **SQLAlchemy'ye ve MySQL'e bağlı
değildir**: hangi verinin hangi tabloya, hangi anahtarlarla ve hangi kolon
kapsamıyla yazılacağını tanımlar. Yazmanın *nasıl* yapıldığı ayrı bir modülde
durur (`persistence.py`, §6.5).

```python
@dataclass(frozen=True)
class TableWrite:
    table: str
    rows: list[dict]
    key_columns: tuple[str, ...]      # doğrulama sorgusu bunları kullanır
    update_columns: tuple[str, ...]   # ON DUPLICATE KEY UPDATE kapsamı
    mode: Literal["upsert", "replace_scope"] = "upsert"

@dataclass(frozen=True)
class NormalizedResult:
    writes: list[TableWrite]          # çok tablolu dataset'ler için
    skipped: dict[str, int]           # hash değişmediği için yazılmayanlar

@dataclass
class WriteStats:
    attempted: dict[str, int]         # tablo → gönderilen satır
    verified: dict[str, int]          # tablo → DB'de varlığı teyit edilen
    skipped: dict[str, int]

class Dataset[RawT](ABC):
    name: str
    depends_on: tuple[str, ...]
    produces: tuple[str, ...]         # yazdığı TABLO adları

    @abstractmethod
    def fetch(self, ctx: SyncContext) -> RawT: ...
    @abstractmethod
    def normalize(self, raw: RawT, symbol: str) -> NormalizedResult: ...

    def upsert(self, writer: RowWriter, result: NormalizedResult) -> WriteStats:
        """Varsayılan uygulama; yalnızca `info`, `fast_info` ve `news`
        bunu geçersiz kılar."""
```

Üç düzeltme, incelemede tespit edilen sözleşme ihlallerini kapatır:

1. **`produces` her zaman tablo adıdır.** `isin` dataset'i `("symbols",)`
   bildirir; hangi kolonlara yazdığı `TableWrite.update_columns`'ta durur.
2. **`upsert` tablo bazlı istatistik döner**, tek `int` değil — `info` 3,
   `news` ve `fast_info` 2 tabloya yazar.
3. **`update_columns` zorunludur.** `symbols` ve `isin` aynı satıra yazar;
   kolon sahipliği dataset başına tekil olmazsa ikinci `symbols` çalıştırması
   `isin` değerini NULL'a ezer ve idempotency vaadini sessizce bozar.
   `symbols` dataset'i `isin` kolonuna **hiç dokunmaz**.

Üçü de yerinde duruyor; aşağıdaki üç madde bunlara sonradan eklendi.

4. **`upsert` `RowWriter` alır, `Session` değil.** Sözleşmenin somut bir
   veritabanına bağlanması SRP ve DIP'i birlikte ihlal ediyordu: "dataset
   yazan" kişi MySQL diyalektini içeren bir modülü import etmek zorunda
   kalıyor, yazma mantığını test etmek gerçek bir MySQL gerektiriyordu.
   Protokol arkasına alındıktan sonra snapshot hash-atlama mantığı,
   `news_symbols.is_known` doldurma ve okuma/yazma sıra garantisi
   **veritabanısız** test edilebilir hâle geldi (§9.1).
5. **`Dataset` generic'tir (`Dataset[RawT]`).** Daha önce `fetch` `Any`
   döndürüyor ve iki adım arasında ad-hoc sözlükler dolaşıyordu
   (`{"info": ..., "fetched_at": ...}`); `mypy --strict` bu sınırda hiçbir şey
   doğrulayamıyordu. Payload tipleri artık `datasets/payloads.py` içinde
   tanımlı (`SymbolsPayload`, `MetadataPayload`, `InfoPayload`,
   `FastInfoPayload`, `SeriesPayload`, `FramePayload`, `NewsPayload`) ve
   `fetch` → `normalize` sözleşmesi derleme zamanında denetleniyor. Bu
   değişiklik hemen bir kusur yakaladı: `info` `None` olabilirken
   `dict(info)` çağrılıyordu.
6. **`produces` bir class attribute'tur, property değil.** Seri dataset'leri
   (`dividends`, `splits`, `capital_gains`) onu ortak tabanda property'ye
   çevirip tabanın sözleşmesini daraltıyordu (LSP ihlali, `type: ignore` ile
   susturulmuştu). Her biri artık kendi `produces` değerini açıkça bildirir.

`mode="replace_scope"` yalnızca `company_officers` için kullanılır: sembole
ait mevcut satırlar silinip yeniden yazılır, böylece şirketten ayrılan
yönetici kaydı kalıcı olarak durmaz.

**Boş liste silmez.** Yahoo `companyOfficers`'ı boş döndürdüğünde mevcut
satırlar korunur. Kaynağın geçici bir boş yanıtı gerçek veriyi silmemelidir;
`empty` bir yokluk bildirimi değil, bilgi yokluğudur (§8.2 ile aynı ilke).
Silme yalnızca dolu bir listenin içinden düşen kayıtlar için gerçekleşir.

**`SyncContext`** sembol-kapsamlıdır ve **worker'a aittir, paylaşılmaz**:
`symbol`, taze `yf.Ticker`, `fetched_at`, salt-okunur watermark sağlayıcı
(`ctx.watermark(table, column)`), ve `ctx.cached(key, fn)` fetch önbelleği.
Paylaşılmadığı için `cached` üzerinde kilide gerek yoktur; sembol işlendikten
sonra bütünüyle atılır.

### 6.2 Registry

```python
REGISTRY: dict[str, Dataset[Any]] = {}
ALIASES: dict[str, tuple[str, ...]] = {"actions": ("dividends", "splits", "capital_gains")}

def register(ds: Dataset[Any]) -> Dataset[Any]:
    REGISTRY[ds.name] = ds
    return ds

def resolve(names: Sequence[str] | None) -> list[Dataset[Any]]:
    """None veya 'all' → hepsi. Alias'ları genişletir, SIRA KORUNARAK
    tekilleştirir, depends_on'a göre topolojik sıralar, bilinmeyen adı
    reddeder, döngüyü yakalar. 'symbols' her zaman başa eklenir."""
```

`src/yfin/datasets/__init__.py` alt modülleri import ederek kayıtları tetikler.

### 6.3 Kayıtlı dataset'ler

| # | `name` | `depends_on` | yfinance çağrısı | `produces` |
|---|---|---|---|---|
| 0 | `symbols` | — | `fast_info` + `get_history_metadata()` | `symbols` |
| 1 | `isin` | `symbols` | `get_isin()` | `symbols` (yalnız `isin` kolonu) |
| 2 | `history` | `symbols` | `history(period="max" \| start=…, interval="1d", auto_adjust=False, actions=True)` | `price_history` |
| 3 | `history_metadata` | `symbols` | `get_history_metadata()` | `history_metadata` |
| 4 | `dividends` | `symbols` | `get_dividends(period="max")` | `dividends` |
| 5 | `splits` | `symbols` | `get_splits(period="max")` | `splits` |
| 6 | `capital_gains` | `symbols` | `get_capital_gains(period="max")` | `capital_gains` |
| 7 | `shares_full` | `symbols` | `get_shares_full(start="1970-01-01")` | `shares_full` |
| 8 | `info` | `symbols` | `get_info()` | `ticker_info`, `ticker_info_history`, `company_officers` |
| 9 | `fast_info` | `symbols` | `get_fast_info()` | `ticker_fast_info`, `ticker_fast_info_history` |
| 10 | `news` | `symbols` | `get_news(count=50, tab="all")` | `news`, `news_symbols` |

**Alias `actions`** → `("dividends", "splits", "capital_gains")`. Veri
`v_actions` view'ından okunur, ayrıca saklanmaz.

**Zorunlu notlar:**

- `shares_full`'da **`start` verilmesi zorunludur.** `start=None` yfinance
  içinde `end - 548 gün` olur ve AAPL'de 420 satırın 353'ü sessizce kaybolur.
  Incremental modda `start = MAX(as_of_date) - 7 gün`.
- `news` dataset'i **taze bir `yf.Ticker` kullanır.** `get_news`'ün önbelleği
  `count`/`tab` parametrelerini yok sayar (`if self._news: return self._news`);
  aynı Ticker daha önce `news` çekmişse çağrı sessizce 10 haberle sınırlanır.
  Bu, `ctx.cached` mekanizmasının tek istisnasıdır.
- `capital_gains` kendi `get_capital_gains(period="max")` çağrısını yapar.
  yfinance bunu Ticker-içi geçmiş önbelleğinden karşıladığı için pratikte
  çoğu zaman ikinci bir ağ isteği doğmaz, ama bu **garanti değildir**:
  incremental modda `history` farklı bir `start` ile çağrıldığından
  önbellek anahtarı tutmayabilir. Bu yüzden `ctx.cached` üzerinden
  paylaşım kurulmaz; bağımsız çağrı bilinçli bir tercihtir.
- `symbols`, `fast_info` ve `history_metadata` ile aynı iki çağrıyı paylaşır;
  `ctx.cached` tekrar isteği önler.
- `company_officers` `info`'nun ham çıktısından türer, ayrı ağ çağrısı yok.

### 6.4 Yeni API ekleme yolu

`datasets/` altına tek dosya + `models/` altına tablo + Alembic migration.
`runner.py`, `cli.py`, `persistence.py` ve mevcut dataset'ler değişmez.
Payload tipi ihtiyaç duyuluyorsa `datasets/payloads.py`'ye bir dataclass,
yeni bir kolon tipi gerekiyorsa `models/kinds.py`'ye bir satır eklenir.

### 6.5 Yazma katmanı

`persistence.py` dataset'lerin gördüğü tek yazma arayüzünü tanımlar:

```python
class RowWriter(Protocol):
    def write(self, write: TableWrite) -> int: ...          # doğrulanmış satır
    def current_hash(self, table: str, symbol: str) -> str | None: ...
    def known_symbols(self, candidates: set[str]) -> set[str]: ...
```

`MySQLRowWriter` bunun tek üretim uygulamasıdır ve `ON DUPLICATE KEY UPDATE`
kurulumunu, `replace_scope` silmesini ve §8.6'daki anahtar varlığı sorgusunu
kapsar. `runner.py` sembol transaction'ını açtıktan sonra writer'ı kurar.

**Kolon hizalama.** `ON DUPLICATE KEY UPDATE` yalnızca INSERT'te yer alan
kolonlara referans verebilir (`ERROR 1054`). Kaynak kolon seti sembole göre
değiştiği için (fon olmayan sembolde `Capital Gains` yok) `update_columns`
gönderilen satırların kolonlarıyla **kesiştirilir**. Ayrıca SQLAlchemy çok
satırlı INSERT'te ilk satırın anahtarlarını kullandığından, satırlar aynı
kolon setine hizalanır; aksi hâlde farklı kolonlar sessizce düşerdi.

### 6.6 Alan tanımlarının tek kaynağı

Bir alanın **SQL kolon tipi**, **kaynak değerinin dönüşümü** ve **satır
boyutu maliyeti** tek bir tabloda (`models/kinds.py`) durur. Bunlar ayrı
if-zincirlerine dağıldığında yeni bir tip eklemek birden fazla dosyayı
değiştirmeyi gerektiriyor ve sessizce ayrışabiliyorlardı — `dt` tipi
eklenirken bu bizzat yaşandı (üç dosyada üç ayrı değişiklik).

`models/fields.py` hangi kaynak anahtarının hangi kolona ve hangi tiple
gideceğini listeler; `models/columns.py` ve `datasets/common.convert_field`
ikisi de bu tabloyu okur. Bir testi ihlal etmeden ayrışmaları mümkün değildir
(§9.1).

---

## 7. Veri akışı

### 7.1 Akış ve paralellik ekseni

**Paralellik ekseni sembol'dür.** Worker `fetch` + `normalize` yapar (ağ ve
saf dönüşüm); DB yazımı ana thread'de, sembol başına tek transaction'da
gerçekleşir.

```
CLI --symbols AAPL,THYAO.IS --datasets all
  │
  ├─ MySQL advisory lock alınır (GET_LOCK), alınamazsa çıkış kodu 4
  │
  ├─ ThreadPoolExecutor(max_workers=4) — sembol başına bir görev:
  │     ctx = SyncContext(symbol, Ticker(symbol), fetched_at)
  │     RESOLVE: fast_info + history_metadata → symbols satırı
  │              çözülemezse → unknown_symbol, sembol atlanır
  │     her dataset: fetch(ctx) → normalize(raw, symbol)
  │     WORKER sonucu kuyruğa KENDİSİ koyar (maxsize=8 → backpressure)
  │
  └─ Ana thread kuyruğu tüketir:
        sembol başına TEK transaction içinde tüm upsert'ler
        → sync_run_items (tablo başına bir satır)
```

**Bellek sınırı.** `period="max"` günlük seri sembol başına on binlerce satır
üretir (^GSPC: 24 786). Kuyruk `maxsize=8` ile sınırlıdır ve **sınırın
bağlayıcı olması için sonucu kuyruğa worker'ın kendisi koyar**: kuyruk
dolduğunda worker `put()` üzerinde bloke olur ve yeni sembol çekilmez.

Sonuçları ana thread'de `future.result()` ile toplamak bu garantiyi **vermez**
— ölçülerek doğrulandı. `pool.submit()` tüm sembolleri anında kabul eder;
worker'lar dört dört çalışmayı sürdürür ve tamamlanan payload'lar `Future`
nesnelerinde birikir. Tüketici yavaşsa bellek sembol sayısıyla doğru orantılı
büyür. 200 sembollük ölçümde eski yaklaşım tüketici hiç tüketmezken sembollerin
tamamını işledi; worker-put yaklaşımı `maxsize + worker` sayısında durdu (§9.1).

**Bağlantı havuzu.** Aynı anda bağlantı isteyenler: sembol transaction'ı,
watermark okuyucusu ve advisory lock'u tutan oturum. SQLAlchemy'nin varsayılan
`pool_size=5` değeri bu toplamda sınırda kalıp `QueuePool timeout` üretiyordu;
havuz `YF_MAX_WORKERS`'a göre boyutlandırılır.

**Thread-safety.** `SyncContext` worker'a özeldir, paylaşılmaz. Token-bucket
rate limiter **process-global ve kilitlidir**; `tenacity` retry'ları da
limiter'dan geçer.

### 7.2 Idempotency

Her yazma `INSERT ... ON DUPLICATE KEY UPDATE`
(`sqlalchemy.dialects.mysql.insert(...).on_duplicate_key_update(...)`),
`update_columns` ile kapsamı sınırlı. Aynı komutu 10 kez çalıştırmak 1 kez
çalıştırmaya eşdeğerdir.

Snapshot tabloları (`ticker_info`, `ticker_fast_info`) satırı günceller.
`_history` tabloları yalnızca `content_hash` değiştiyse yeni satır ekler;
değişmediğinde bu bir `skipped`'tır, hata değil.

**`content_hash` kanonik JSON üzerinden hesaplanır:**
`json.dumps(payload, sort_keys=True, allow_nan=False, ensure_ascii=False,
separators=(",",":"))` → SHA-256. Hash **her zaman Python tarafında**
hesaplanır; aynı kanonik string `raw_json` LONGTEXT kolonuna yazılır, böylece
DB'den okunan gövdeden hash yeniden doğrulanabilir.

### 7.3 Incremental çekim

`fetch`, `ctx.watermark(...)` ile salt-okunur bir DB sorgusu yapabilir —
sözleşme buna açıkça izin verir.

- `history`: ilk çekimde `period="max"`; sonra
  `start = MAX(session_date) - 7 gün`.
- `shares_full`: ilk çekimde `start="1970-01-01"`; sonra
  `start = MAX(as_of_date) - 7 gün`.

Yedi günlük örtüşme Yahoo'nun geriye dönük düzeltmelerini (revize kapanış,
geç bildirilen temettü) yakalar; upsert düzeltilmiş satırı üstüne yazar.
`--full-refresh` bu watermark'ları atlar; snapshot `content_hash` mantığını
**etkilemez**.

### 7.4 Toplu çekim neden yok

`Tickers` / `yf.download` ile çok sembollü `history` çekimi tasarımdan
bilinçli olarak çıkarıldı. Dört ilkeyle birden çatışıyordu: tek sembollü
`fetch` imzası; (sembol × dataset) hata sınırı (tek istek patlarsa N sembol
birden düşer); sembol başına tek transaction; ve sembol başına farklı
incremental `start` (`yf.download` tek `start` alır → ya aşırı çekim ya
watermark'ların yok sayılması). İleride gerekirse ayrı bir `BatchDataset`
sözleşmesiyle eklenir.

### 7.5 Rate limiting

- Process-global, kilitli token-bucket: varsayılan 2 istek/sn (config).
- `tenacity` ile 429/5xx/timeout üzerinde exponential backoff: 5 deneme,
  1→16 sn, jitter'lı (üçü de config'ten; testte bekleme sıfırlanır).
  Retry'lar da limiter'dan geçer.
- `curl_cffi` oturumu (yfinance 1.7 bağımlılığı) tarayıcı TLS parmak izi
  taklit ederek engellenme oranını düşürür.

**Retry sınıflandırması.** yfinance hataları tek bir istisna sınıfına inmediği
için karar mesaj + sınıf adı üzerinden verilir, ama **düz metin kalıbı
kırılgandır**: `"503 service"` gibi bir kalıp `RuntimeError("HTTP 503")`
mesajını kaçırır, çıplak `"500"` araması ise `"Symbol 500 not found"` gibi
mesajlarda yanlış eşleşir ve gerçekte kalıcı olan bir hatayı beş kez
tekrarlatır. Bu yüzden HTTP durum kodu (`429`, `5xx`) **ancak bir HTTP
bağlamıyla birlikte** görülürse retry sayılır; gerçek hata mesajları
(`429 Client Error: Too Many Requests for url …`) bu bağlamı her zaman taşır.
`TimeoutError` ve `ConnectionError` doğrudan retry'lanır.

---

## 8. Hata yönetimi ve veri bütünlüğü

### 8.1 İzolasyon sınırı ve durumlar

Hata sınırı **(sembol × dataset)** hücresidir. Her hücre `sync_run_items`'a
yazılır (çok tabloya yazan dataset'ler için tablo başına bir satır):

| Durum | Anlamı |
|---|---|
| `ok` | Veri çekildi ve yazıldı |
| `empty` | Kaynak veri yok — **hata değil** |
| `skipped` | `content_hash` değişmedi, `_history`'ye yazılmadı |
| `failed` | Ağ, normalize veya yazma hatası |
| `unknown_symbol` | Sembol çözülemedi; sembolün tüm dataset'leri atlanır |

**Çıkış kodları:** `failed` yok → **0** (`ok ∪ empty ∪ skipped` normaldir);
hiçbir sembol çözülemedi → **1**; kısmi `failed` → **2**; tüm hücreler
`failed` → **3**; advisory lock alınamadı → **4**.

### 8.2 `empty` ≠ `failed`

`capital_gains` **hiçbir sembolde dolu gelmiyor** — test edilen 7 fon/ETF
dahil. Boşu hata sayan sistem her çalıştırmada yanlış alarm verir; hatayı boş
sayan sistem sessizce veri kaybeder.

### 8.3 Normalizasyon kuralları

| Durum | Kural |
|---|---|
| Sembol girdisi | `strip().upper()` — tek kanonik biçim; `ascii_bin` collation ile birlikte `aapl`/`AAPL` karışıklığını imkânsız kılar |
| Sentinel değerler | `'-'`, `''`, `'N/A'` → `NULL` (ISIN ve `news.display_time` dahil) |
| `NaN` / `NaT` / `pd.NA` | `NULL`; `float('nan')` asla MySQL'e gitmez |
| **Boş sonuç kontrolü** | `raw is None or len(raw) == 0` → `empty`. **`.empty` tek başına yetmez**: `get_shares_full` `None` döndürebilir (`None.empty` → `AttributeError`) |
| numpy tipleri | Python `int` / `Decimal` / `datetime`'a çevrilir |
| **float → DECIMAL** | **`Decimal(repr(float(x)))`**. Çıplak `Decimal(repr(x))` numpy 2.x'te `InvalidOperation` fırlatır: `repr(np.float64(0.00187))` = `'np.float64(0.00187)'`. `Decimal(float)` ise ikili artık üretir. |
| tz-aware index | `session_date` = **yerel tarih**, `ts_utc` = UTC. `dividends`/`splits` index'i history'den farklı saatte gelir (THYAO: 09:30 vs 00:00); `ex_date` de yerel tarihtir |
| **`HistoryMetadata`** | `dict` **değil**, `Mapping`. `isinstance(x, dict)` yanlış dallanır. `raw_json` için özel encoder zorunlu: `tradingPeriods` (**DataFrame**) → `.reset_index().to_dict("records")`, tüm `Timestamp` → ISO 8601, `default=str` fallback. Düz `json.dumps` `TypeError` verir |
| **Kaynak → `dict` dönüşümü** | `dict(raw)` **kullanılamaz**: `HistoryMetadata` Mapping sözleşmesini ihlal eder (§4.2), `keys()` ile `__getitem__` uyuşmaz ve VFIAX'te `KeyError` fırlatır. Anahtarlar tek tek okunur, çözülemeyen anahtar atlanıp `WARNING: unreadable source keys` loglanır (`normalize.as_mapping`) |
| `history_metadata` zaman alanları | `firstTradeDate` ve `regularMarketTime` epoch **değil**, yfinance'ın çevirdiği `Timestamp`'tir; epoch olarak yorumlamak sessizce `NULL` üretir. Ayrı bir tip (`dt`) ile ele alınır — hem `Timestamp` hem epoch kabul eder |
| `raw_json` üretimi | `json.dumps(..., sort_keys=True, allow_nan=False, ensure_ascii=False)`. `allow_nan=True` (varsayılan) ile `NaN` sızarsa MySQL `ERROR 3140` verir ve §8.7 gereği **tüm sembolün transaction'ı geri alınır** |
| `history` kolonları | Kolon seti sembole göre değişir (fon/ETF'te `Capital Gains` eklenir); sabit sıraya/varlığa güvenilmez |
| Nested listeler | `companyOfficers` tabloya; `corporateActions`, `executiveTeam`, `maxAge` `raw_json`'da |
| `company_officers.name` | `" ".join(name.split())` — kaynakta çift boşluk var (`"Mr. Kevan  Parekh"`); normalize edilmezse Yahoo boşluğu değiştirdiğinde duplike satır oluşur |
| `news` thumbnail | `tag="original"` çözünürlüğü kolonlara; `thumbnail` `None` olabilir |

### 8.4 Epoch alan haritası (21 alan)

Birim tahmin edilmez; harita zorunludur.

**Milisaniye:** `firstTradeDateMilliseconds`

**Saniye:** `exDividendDate`, `dividendDate`, `lastDividendDate`,
`earningsTimestamp`, `earningsTimestampStart`, `earningsTimestampEnd`,
`earningsCallTimestampStart`, `earningsCallTimestampEnd`, `lastFiscalYearEnd`,
`nextFiscalYearEnd`, `mostRecentQuarter`, `lastSplitDate`,
`governanceEpochDate`, `compensationAsOfEpochDate`, `dateShortInterest`,
`sharesShortPreviousMonthDate`, `preMarketTime`, `regularMarketTime`,
`fundInceptionDate`, `startDate`

**Epoch DEĞİL** (adı çağrıştırsa da dönüştürülmez): `fullTimeEmployees` (int),
`allTimeHigh` / `allTimeLow` (float fiyat), `isEarningsDateEstimate` (bool),
`exchangeTimezoneName` / `exchangeTimezoneShortName` (str).

Haritada olmayan ve epoch aralığında görünen bir alan bulunursa
`WARNING: unmapped epoch-like key: <ad>` loglanır (§8.5 ile aynı mantık).

### 8.5 Yeni alan kaçırmama

`info` normalizasyonu, tipli kolonlara yazdıktan sonra haritalanmamış
anahtarları `WARNING: unmapped info keys: [...]` olarak loglar. Ham veri
`raw_json`'da olduğu için **veri kaybı yoktur**; log yalnızca terfi
sinyalidir. Terfi kararı §5.4'teki 65535 byte satır bütçesi kontrol edilerek
verilir.

### 8.6 Reconciliation

`ON DUPLICATE KEY UPDATE`'in `ROW_COUNT()` değeri **doğrulama için
kullanılamaz**: yeni satır 1, güncellenen 2, **değişmeyen 0** döner (MySQL'de
ölçüldü). "fetched == written" kuralı bu yüzden her idempotent tekrarda
`failed` verirdi. Üstelik değer `CLIENT_FOUND_ROWS` bayrağına bağlı olduğu
için sürücü konfigürasyonuna göre değişir.

Doğrulama bunun yerine **anahtar varlığı sorgusuyla** yapılır:

```
rows_attempted = len(TableWrite.rows)
rows_verified  = SELECT COUNT(*) FROM <table> WHERE <key_columns> IN (gönderilen anahtarlar)
```

Bir hücre `ok` sayılır ancak ve ancak `rows_verified == rows_attempted`.
`rows_skipped` (hash değişmemesi) ayrı sayaçta tutulur ve bu eşitliğe
girmez. Run düzeyinde toplamlar `sync_runs`'a yazılır; herhangi bir hücrede
eşitlik bozulursa o hücre `failed` olur ve run `partial` (çıkış kodu 2)
biçimini alır.

"Eksiksizlik" iddiasının makine tarafından doğrulanabilir hâli budur.

### 8.7 Transaction sınırı ve eşzamanlılık

Sembol başına tek transaction: bir sembolün verisi ya bütün olarak yazılır ya
hiç. Yarım yazılmış sembol durumu oluşmaz.

Sync başlangıcında MySQL advisory lock (`GET_LOCK('yfin_sync', 0)`) alınır.
Alınamazsa süreç çıkış kodu 4 ile sonlanır — böylece gecikmiş bir cron
tetiklemesi çalışan sync'in üzerine binmez ve iki açık `sync_runs` kaydı
oluşmaz.

Kilit **CLI katmanında değil, `run_sync()` içinde** alınır: hat kütüphane
olarak veya testten doğrudan çağrıldığında da korunmalıdır. Kilidi zaten
dışarıda tutan bir çağıran için `acquire_lock=False` parametresi vardır.

### 8.8 Delisted sembol politikası

Ardışık 5 çalıştırmada `unknown_symbol` alan sembol `is_active=0` yapılır ve
varsayılan sync kapsamının dışına çıkar. `--include-inactive` bayrağı bunları
yine de dener. Veri **silinmez** (§5.5).

---

## 9. Test stratejisi

### 9.1 Normalizasyon testleri (fixture, ağsız)

`scripts/capture_fixtures.py` gerçek API'den bir kez veri çekip
`tests/fixtures/{symbol}/{dataset}.json` olarak kaydeder.

Referans semboller — kapsamı §4.3 matrisine göre seçildi:

| Sembol | Kapsadığı kenar durum |
|---|---|
| `AAPL` | Tüm alanlar dolu, temettü + split, 187 info alanı, `shares_full` duplike tarihler |
| `THYAO.IS` | Sentinel ISIN, pozitif UTC ofseti, farklı para birimi, 161 alan |
| `SPY` | ETF: `shares_full` `None`, `history`'de `Capital Gains` kolonu |
| `BTC-USD` | Kripto: `fast_info.marketCap` ve `.shares` `None`, ISIN `'-'` |

`capital_gains` fixture'ı **elle üretilmiş sentetiktir** — Yahoo hiçbir
sembolde dolu döndürmüyor, canlı yakalanamaz.

§8.3'teki her kural için ayrı test, özellikle:
- `Decimal(repr(float(np.float64(0.00187))))` — **numpy skaler girdiyle**;
  düz `float` ile yazılan test bu hatayı yakalamaz
- `HistoryMetadata` encoder'ı: `tradingPeriods` DataFrame'i ve tz-aware
  `Timestamp`'lerle `json.dumps` başarılı
- `None` dönüşünün `empty` olduğu (`AttributeError` değil)
- Pozitif ofsetli borsada `session_date`'in geri kaymadığı
- 21 epoch alanının doğru birimle çözüldüğü
- `raw_json`'ın `allow_nan=False` ile üretildiği ve `NaN` içermediği
- Mapping sözleşmesini ihlal eden bir kaynağın (`keys()` ≠ `__getitem__`)
  sembolü düşürmediği — hem `as_mapping` hem dataset düzeyinde

Aynı katmanda, **veritabanı gerektirmeyen** üç test grubu daha vardır:

- **Yazma sözleşmesi.** `RowWriter` protokolünün bellek içi bir uygulamasıyla
  (`FakeWriter`) snapshot hash-atlama mantığı, `news_symbols.is_known`
  doldurma ve evren dışı sembolün transaction'ı düşürmediği doğrulanır.
  Kritik bir sıra garantisi de buradadır: **hash, snapshot güncellenmeden
  önce okunmalıdır** — tersi hâlinde karşılaştırma her zaman eşitlenir ve
  `_history` hiçbir zaman yeni satır almaz.
- **Backpressure.** Tüketici hiç tüketmezken kaç sembolün işlendiği ölçülür;
  worker-put stratejisi kuyruk sınırında durmalı, `future.result()` stratejisi
  durmamalıdır (§7.1).
- **Rate limit ve retry.** Token-bucket'ın hızı uyguladığı, kilit altında
  eşzamanlı çağrılarda tutarlı kaldığı, retry'ların limiter'dan geçtiği ve
  §7.5'teki sınıflandırmanın gerçek hata mesajlarında doğru karar verdiği
  (yanlış pozitifler dâhil).

- **Alan tablosu tutarlılığı.** Her `Field` bir model kolonuna karşılık gelir,
  kaynak anahtarı ve kolon adı tekrarsızdır, epoch alanları epoch tipiyle
  işaretlidir ve satır bütçesi (§5.4) aşılmaz.
- **Purge kapsamı.** `yfin symbols purge`'ün sildiği tablo kümesi FK
  grafiğinden türetilir ve metadata ile birebir eşleşmelidir; silme sırası
  çocuktan ebeveyne olmalıdır. Elle tutulan bir liste yeni bir tablo
  eklendiğinde sessizce eksik kalırdı.

### 9.2 Repository testleri (gerçek MySQL, ağsız)

Docker'daki MySQL 8.3'te `yfinance_test` şemasına karşı; her test kendi
transaction'ında, sonunda rollback.

Kritik iddialar:
- Aynı satır kümesini iki kez yazmak tabloyu değiştirmez (idempotency)
- Değişen değer üstüne yazılır; `update_columns` dışındaki kolonlara
  dokunulmaz — özellikle `symbols` çalıştırmasının `isin`'i ezmediği
- `rows_verified == rows_attempted` doğrulamasının `ROW_COUNT()` yerine
  anahtar varlığını kullandığı ve ikinci çalıştırmada da `ok` verdiği
- DECIMAL yuvarlama kaybı olmadığı (DB'den geri okunarak)
- `DATETIME(6)` ile aynı saniyede iki snapshot yazılabildiği
- `ascii_bin` collation'ın `'aapl'` ile `'AAPL'`'ı ayırdığı
- `ON DELETE RESTRICT`'in sembol silmeyi engellediği
- `raw_json` LONGTEXT'in byte-for-byte geri okunduğu ve hash'in eşleştiği

### 9.3 Canlı entegrasyon testi (`-m live`)

Varsayılan olarak atlanır. `pytest -m live` ile gerçek Yahoo API + gerçek
MySQL: §9.1'deki dört sembol, 11 dataset, uçtan uca. Ardından:
satır sayıları, §5.2'de NOT NULL işaretli alanların dolu olduğu, FK
bütünlüğü, `v_actions` tutarlılığı.

`capital_gains` için "dolu gelmeli" beklentisi **kurulmaz** — `empty` beklenen
sonuçtur. `shares_full` için BTC-USD'de `empty`, AAPL/THYAO/SPY'de dolu
beklenir (§4.2: `start` verildiğinde SPY veri döndürüyor).

**Eksiksizlik karşılaştırması.** Canlı testin ötesinde, DB'deki satır sayıları
doğrudan kaynağa karşı ölçülür: yedi pazar tipinde sekiz sembol için
`price_history`, `dividends`, `splits`, `shares_full` ve `ticker_info`
sayıları Yahoo'dan yeniden çekilip karşılaştırılır. Beklenen fark **sıfırdır**;
`price_history` değerleri §4.3 matrisiyle de örtüşmelidir.

CI'da değil, elle çalıştırılır.

### 9.4 Statik analiz

`ruff` (lint + format) ve `mypy --strict`. `normalize()` fonksiyonları tip
güvenli olmalıdır. `HistoryMetadata` gibi `dict` **olmayan** `Mapping`'ler
doğru anotasyonlanmalı — yanlış anotasyon mypy'yi de yanıltır.

---

## 10. Proje yapısı

```
~/Projects/learn/yfinance/
├── pyproject.toml              # bağımlılıklar, ruff, mypy, pytest config
├── .env.example
├── alembic.ini
├── migrations/
│   ├── env.py                  # ifade tabanlı indexleri autogenerate'ten hariç tutar
│   └── versions/
├── scripts/capture_fixtures.py
├── src/yfin/
│   ├── config.py               # pydantic-settings, .env okur
│   ├── logging_setup.py        # structlog
│   ├── db.py                   # engine fabrikası, advisory lock
│   ├── client.py               # yfinance sarmalayıcı: retry, rate limit, oturum
│   ├── normalize.py            # nan→None, Decimal, tz, epoch haritası, JSON encoder,
│   │                           #   as_mapping (bozuk Mapping'lere karşı)
│   ├── persistence.py          # RowWriter protokolü + MySQLRowWriter (§6.5)
│   ├── models/
│   │   ├── base.py             # DeclarativeBase, collation ve tip fabrikaları
│   │   ├── fields.py           # kaynak anahtarı → kolon → tip tablosu (§6.6)
│   │   ├── kinds.py            # tip → (SQL tipi, dönüştürücü, satır maliyeti)
│   │   ├── columns.py          # fields + kinds → SQLAlchemy kolonu
│   │   ├── snapshots.py        # ticker_info(_history), ticker_fast_info(_history),
│   │   │                       #   history_metadata (Core Table, fields'ten üretilir)
│   │   └── {symbols,prices,officers,news,sync,views}.py
│   ├── datasets/
│   │   ├── base.py             # Dataset ABC, TableWrite, NormalizedResult, WriteStats
│   │   │                       #   (SQLAlchemy'ye bağlı DEĞİL)
│   │   ├── payloads.py         # fetch → normalize arası tipli sözleşmeler
│   │   ├── common.py           # alan projeksiyonu, snapshot satırı, unmapped uyarısı
│   │   ├── snapshot_base.py    # content_hash'e göre _history atlama
│   │   ├── registry.py
│   │   └── {symbols,isin,history,history_metadata,corporate_actions,
│   │         shares_full,info,fast_info,news}.py
│   ├── runner.py               # orkestrasyon, kuyruk, sync_runs, hata izolasyonu
│   └── cli.py                  # typer
├── tests/
│   ├── fixtures/{AAPL,THYAO.IS,SPY,BTC-USD,SYNTHETIC}/
│   ├── helpers.py              # fixture yükleme ve rehydration
│   ├── unit/  repo/  live/
│   └── conftest.py
└── docs/superpowers/specs/
```

`dividends`, `splits` ve `capital_gains` tek dosyadadır
(`corporate_actions.py`): üçü de aynı şekle sahiptir (tarih indeksli tek
sütunlu `Series`) ve ortak bir tabandan türer.

Klasör adı `yfinance` olsa da Python paketi `src/yfin/` altındadır; src-layout
sayesinde gerçek `yfinance` kütüphanesiyle import çakışması olmaz.

---

## 11. Yapılandırma ve kullanım

```
DB_HOST=localhost              # §3: 127.0.0.1'de başka bir MySQL dinliyor
DB_PORT=3306
DB_USER=root
DB_PASSWORD=root
DB_NAME=yfinance
DB_TEST_NAME=yfinance_test
YF_RATE_LIMIT_PER_SEC=2
YF_MAX_WORKERS=4
YF_QUEUE_MAXSIZE=8
YF_RETRY_ATTEMPTS=5
YF_RETRY_INITIAL_SEC=1
YF_RETRY_MAX_SEC=16
YF_NEWS_COUNT=50
YF_NEWS_TAB=all
YF_INCREMENTAL_OVERLAP_DAYS=7
YF_DELIST_THRESHOLD=5
LOG_LEVEL=INFO
```

Şifre koda girmez; `.env` versiyon kontrolüne girmez.

```bash
yfin db create                             # şemaları oluşturur (yoksa)
yfin db upgrade                            # alembic migration
yfin db revision -m "..."                  # modellerden migration üretir
yfin datasets                              # kullanılabilir dataset adları
yfin symbols add AAPL MSFT THYAO.IS        # strip().upper() uygulanır
yfin symbols list [--include-inactive]
yfin symbols deactivate SYMBOL             # soft delete (§5.5)
yfin sync --datasets all
yfin sync --symbols AAPL --datasets info,news
yfin sync --symbols AAPL --full-refresh
yfin status [--limit N]                    # son sync run özeti + failed hücreler
yfin prune                                 # öksüz news satırlarını siler
yfin symbols purge SYMBOL --force          # gerçek silme (veri kaybı)
```

### Bağımlılıklar

**Çalışma zamanı:** `yfinance`, `sqlalchemy>=2`, `alembic`, `pymysql`,
`cryptography`, `pydantic-settings`, `typer`, `tenacity`, `structlog`

**Geliştirme:** `pytest`, `ruff`, `mypy`

---

## 12. Versiyonlama

Git deposu ve commit'ler kullanıcı tarafından yönetilir. Bu proje `git init`
çalıştırmaz.

---

## Ek A — İnceleme geçmişi

Bu doküman üç bağımsız incelemeden geçti (2026-09-04): mimari tutarlılık,
MySQL şema doğrulaması (gerçek MySQL 8.3 üzerinde deneylerle) ve yfinance API
doğrulaması (canlı çağrılarla).

**Kapatılan kritik bulgular:**

| Bulgu | Nerede düzeltildi |
|---|---|
| Reconciliation `ROW_COUNT()` ile her idempotent tekrarda `failed` verirdi | §8.6 — anahtar varlığı sorgusu |
| `get_shares_full(start=None)` verinin %84'ünü sessizce kaybediyordu | §6.3 — `start="1970-01-01"` |
| `shares_full` PK'sı aynı tarihteki farklı değerleri eziyordu | §5.2 — PK'ya `shares` eklendi |
| `Decimal(repr(x))` numpy 2.x'te her fiyat satırında çökerdi | §8.3 — `Decimal(repr(float(x)))` |
| `HistoryMetadata` `dict` değil; `json.dumps` `TypeError` verirdi | §8.3 — özel encoder |
| `get_shares_full` `None` dönüşü `AttributeError` verirdi | §8.3 — `raw is None or len(raw) == 0` |
| `sync_run_items` FK'sı `unknown_symbol` kaydını imkânsız kılıyordu | §5.5 — FK kaldırıldı |
| `news_symbols` FK'sı evren dışı sembollerde transaction'ı düşürürdü | §5.5 — FK kaldırıldı, `is_known` eklendi |
| `fetched_at` `DATETIME(0)` ile PK çakışması ve yuvarlama | §5.4 — tüm damgalar `DATETIME(6)` |
| `utf8mb4_0900_ai_ci` sembol PK'sında `AAPL` = `aapl` | §5.1 — `ascii_bin` |
| `raw_json` `JSON` tipi hassasiyet ve sıra kaybı, `NaN`'da `ERROR 3140` | §5.4 — `LONGTEXT` + kanonik dump |
| `produces`/`upsert` sözleşmesi çok tablolu ve kolon bazlı yazımı taşımıyordu | §6.1 — `TableWrite`, `WriteStats` |
| `symbols` çalıştırması `isin`'i NULL'a ezerdi | §6.1 — `update_columns` zorunlu |
| Paralellik ekseni tanımsız, `SyncContext` thread-safety'si havada | §7.1 — sembol ekseni, worker-özel context |
| `history` toplu çekimi dört mimari ilkeyle çatışıyordu | §7.4 — kaldırıldı |
| `empty` durumu çıkış kodu 0'ı imkânsız kılıyordu | §8.1 — `failed` yoksa 0 |
| `ON DELETE CASCADE` denetim kaydını ve 40 yıllık geçmişi siliyordu | §5.5 — `RESTRICT` + soft delete |
| Öksüz `news` satırları hiç temizlenmiyordu | §5.5 — `yfin prune` |
| Epoch haritası 21 alandan 3'ünü kapsıyordu | §8.4 — tam liste |
| tz gerekçesi yanlış borsayı işaret ediyordu | §5.4 — pozitif ofsetli borsalar |
| Sembol normalizasyonu tanımsızdı | §8.3 — `strip().upper()` |
| Run eşzamanlılık kilidi yoktu | §8.7 — `GET_LOCK` |
| Nullability hiç tanımlanmamıştı | §5.2 |
| `ticker_info` satır bütçesi ~63 kolonda doluyor | §5.4 — bütçe kuralı |
| `get_news` önbelleği `count`/`tab`'ı yok sayıyor | §6.3 — taze `Ticker` |
| `company_officers` ayrılan yöneticiyi silmiyordu | §6.1 — `replace_scope` |
| Eksik indeksler (`v_actions` tarih sorgusu full scan) | §5.6 |

## Ek B — Uygulama sırasında kapatılan bulgular

Aşağıdakiler doküman incelemesinde değil, **kod yazılıp çalıştırılırken**
ortaya çıktı. Her biri çalışan sistemde ölçülerek doğrulandı ve bir testle
bağlandı.

| Bulgu | Belirti | Nerede düzeltildi |
|---|---|---|
| `HistoryMetadata` Mapping sözleşmesini ihlal ediyor | VFIAX'in **11 dataset'i birden** `unknown_symbol` oluyordu | §4.2, §8.3 — `normalize.as_mapping` |
| Kuyruk `maxsize`'ı üretimi sınırlamıyordu | `future.result()` ile toplanan payload'lar `Future`'larda birikiyordu; bellek sembol sayısıyla büyürdü | §7.1 — sonucu worker kuyruğa koyar |
| `ON DUPLICATE KEY UPDATE` kolon uyuşmazlığı | `update_columns`'ta olup INSERT'te olmayan kolon `ERROR 1054`; `Adj Close` gelmeyen sembollerde `history` de düşerdi | §6.5 — kesişim + satır hizalama |
| Snapshot hash'i güncellemeden **sonra** okunuyordu | Karşılaştırma her zaman eşitleniyor, `_history` hiç yeni satır almıyordu | §6.1/4, §9.1 — sıra testle bağlandı |
| Worker çökerse denetim kaydı hiç yazılmıyordu | Sembol `sync_run_items`'ta hiç görünmüyordu | §8.1 — çöken worker da `unknown_symbol` yazar |
| `history_metadata` zaman alanları epoch sanılıyordu | `first_trade_date` ve `regular_market_time` sessizce `NULL` | §8.3 — `dt` tipi |
| Retry sınıflandırması metin kalıbına dayanıyordu | `HTTP 503` kaçırılıyor, `Symbol 500 not found` yanlışlıkla 5 kez deneniyordu | §7.5 — durum kodu + HTTP bağlamı |
| Bağlantı havuzu worker sayısına göre boyutlanmamıştı | Canlı testte `QueuePool timeout` | §7.1 — havuz `YF_MAX_WORKERS`'a bağlı |
| Sözleşme MySQL diyalektine bağlıydı (SRP/DIP) | Yazma mantığı yalnızca gerçek MySQL ile test edilebiliyordu | §6.1/4, §6.5 — `RowWriter` protokolü |
| `fetch`→`normalize` arasında ad-hoc sözlükler | `mypy --strict` sınırda hiçbir şey doğrulayamıyordu | §6.1/5 — `datasets/payloads.py` |
| Kolon tipi ve dönüştürücü ayrı if-zincirlerindeydi (OCP) | `dt` tipi eklemek üç dosyada değişiklik gerektirdi | §6.6 — `models/kinds.py` |
| `purge` tablo listesi elle tutuluyordu (OCP) | Yeni bir sembol-kapsamlı tablo eklendiğinde FK hatası verirdi | §9.1 — FK grafiğinden türetilir |
| Seri dataset'lerinde `produces` property'ydi (LSP) | Taban sözleşmesini daraltıyor, `type: ignore` ile susturuluyordu | §6.1/6 |

**Ölçülen son durum.** Yedi pazar tipinde sekiz sembol, tüm dataset'ler:
`ok=86, empty=28, failed=0`, çıkış kodu 0. Kaynağa karşı satır karşılaştırması
40 hücrede **fark sıfır**. İkinci çalıştırma yeni satır üretmiyor (`skipped=8`,
hash değişmeyen snapshot'lar). `SHA2(raw_json,256) = content_hash` tüm
snapshot tablolarında tutuyor, `v_actions` taban tabloların toplamına eşit,
öksüz FK kaydı yok.
