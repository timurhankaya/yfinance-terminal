# yfinance Financials + Calendar + Market → MySQL — Tasarım Dokümanı

- **Tarih:** 2026-09-04
- **Durum:** Üç bağımsız incelemeden geçti, revize edildi
- **Proje kökü:** `~/Projects/learn/yfinance/`
- **Önceki spec:** `2026-09-04-yfinance-mysql-etl-design.md` (bu doküman onu **genişletir**, yerine geçmez)
- **Kapsam:** yfinance `Ticker` API'sinin "Financials" + "Calendars" bölümleri ve `yfinance.Market`

---

## 1. Amaç

Kurulu veri hattına (11 dataset, 16 tablo) yfinance'ın **finansal tablo**,
**takvim** ve **piyasa** verilerini eklemek. Mevcut ilkeler değişmez:
eksiksizlik, tip güvenliği, idempotency, sembol kodu üzerinden ilişki,
makine tarafından doğrulanabilir "eksiksiz yazıldı" iddiası.

Bu genişletme mevcut mimarinin iki varsayımını zorluyor; ikisi de açıkça
çözülüyor:

1. **Her dataset sembol-kapsamlıdır.** `yfinance.Calendars` ve
   `yfinance.Market` piyasaya bağlıdır, sembole değil (§6.4).
2. **Her dataset sabit bir kolon setine yazar.** Finansal tabloların kalem
   seti sembole ve sektöre göre değişir: 3 sembolde 237, 10 sembolde 302
   farklı kalem ölçüldü; kütüphanedeki kapalı evren 375 (§4.1). Çözüm uzun
   (EAV) şema (§5.2).

### Kapsam içi

**Sembol-kapsamlı, 13 yeni dataset:**
`income_stmt`, `quarterly_income_stmt`, `ttm_income_stmt`,
`balance_sheet`, `quarterly_balance_sheet`,
`cashflow`, `quarterly_cashflow`, `ttm_cashflow`,
`calendar`, `earnings_dates`, `sec_filings`,
`valuation_measures`, `quarterly_valuation_measures` (Ek B).

**Piyasa-kapsamlı, 6 yeni dataset:**
`market_status`, `market_summary`, `earnings_calendar`, `economic_calendar`,
`ipo_calendar`, `splits_calendar`.

**15 yeni tablo** (3'ü `_history` çifti) + mevcut `sync_runs`'a bir kolon.
Toplam tablo sayısı 16 → **31**.

### Kapsam dışı — gerekçeli, tahminle değil ölçümle

| Çıkarılan | Gerekçe |
|---|---|
| `earnings` / `get_earnings` | **Kaynak garantisi:** `scrapers/fundamentals.py` içindeki `Fundamentals.earnings` property'si koşulsuz `return None` + `DeprecationWarning` üretir. Test edilen 6 sembolde de `None` döndü. Hiç dolmayacak bir tablo açmak olurdu. |
| `ttm_balance_sheet` | `freq="trailing"` bilançoda `ValueError: Illegal argument: frequency 'trailing' only available for cash-flow or income data` (`scrapers/fundamentals.py:80-82`). Ekran görüntüsündeki listede de yok. |
| `eps_trend`, `growth_estimates`, analyst/estimates ailesi | Ayrı bir "Analysis" alt projesi. Bu spec'in EAV şeması onları migration'sız kabul edecek biçimde tasarlandı. |
| `get_valuation_measures` / `valuation` | **Artık kapsam dışı DEĞİL** — bu spec ile AH spec'i arasındaki çatlaktan düşmüştü; sonradan **Ek B** ile eklendi. |
| Takvim verisinin geriye dönük tam arşivi | Yahoo takvim uçları pencere tabanlıdır; "tüm geçmiş" diye bir uç yoktur (§7.4). |

---

## 2. Kararlar ve gerekçeleri

| Karar | Seçim | Gerekçe |
|---|---|---|
| Finansal tablo şeması | Uzun (EAV): `financial_periods` + `financial_facts` | Kalem seti sembole/sektöre göre değişken (10 sembolde 302 etiket); geniş şema her yeni kalemde migration ister |
| Kalem anahtarı | `pretty=False` camelCase (`NormalizedEBITDA`) | Etiketler `const.fundamentals_keys`'ten gelir — kütüphane sürümüne bağlı, Yahoo sunum katmanına değil. `pretty=True` sunum biçimidir |
| `item_key` genişliği | `VARCHAR(128) ascii_bin` | Ölçülen max **60**; kapalı evrenin max'ı da 60. `VARCHAR(64)` yalnızca 4 karakter pay bırakırdı; VARCHAR'da fazla genişlik depolama maliyeti üretmez |
| Değer tipi | `DECIMAL(38,10)`, Python tarafında `quantize(1E-10)` | Aynı tabloda `1.06×10¹⁴` ve `0.156` var. MySQL 11. basamağı **sessizce yuvarlar** (yalnızca `Note 1265`), bu yüzden yuvarlama bilinçli olarak Python'da yapılır |
| `NaN` hücreler | Satır **yazılmaz** | AAPL income'da 195 hücrenin 45'i `NaN`; "kalem o dönemde yok" bilgisi satırın yokluğuyla ifade edilir |
| Dönem tekrar yazımı | `financial_periods.content_hash` ile `financial_facts` atlanır; başlık satırı **her zaman** yazılır | 5 yıllık dönemin 4'ü her çalıştırmada aynı; `fetched_at` "son doğrulama zamanı" olarak ilerlemelidir |
| ENUM tanımı | Tek `StrEnum` + tek `sqlalchemy.Enum` nesnesi, iki tabloda paylaşılır | ENUM listeleri ebeveyn/çocukta ayrışırsa FK **ordinal üzerinden sessizce yanlış satıra bağlanır** (§4.4/M2) |
| Piyasa verisi çalışma modeli | Ayrı komut: `yfin market sync` | Sembol evreninden bağımsız; ayrı advisory lock, sembol sync'iyle eşzamanlı koşabilir |
| Bölge döngüsü | Dataset'in **dışında**, `market_runner` içinde | `_record_items` ve `sync_run_items` granülerliği (dataset × tablo × bölge) böylece mevcut kodla paylaşılır |
| Registry | Generik `Registry` sınıfı, iki örnek | Alias/topolojik sıra/döngü mantığı tek yerde |
| `replace_scope` kapsamı | `TableWrite.scope_columns` + `scope_values` | Bugün sembol kolonu sabit kodlu **ve boş satır listesinde hiç silmiyor** (§6.2) |
| `earnings_dates` anahtarı | `(symbol, earnings_ts_utc, fact_hash)` | Aynı damgada yalnızca `Surprise(%)` ile ayrılan 2 satır ölçüldü |
| `earnings_dates` sayfalama | Her sayfa için **taze `yf.Ticker`** | `TickerBase._earnings_dates` sözlüğü yalnızca `limit`'e anahtarlı; aynı Ticker'da `offset` **yok sayılır** (§4.2) |
| `earnings_calendar` çağrısı | `filter_most_active=False` | Varsayılan `True` yalnızca `offset==0`'da uygulanır; sayfa 0 ile sayfa 1+ **farklı evrenlerden** gelir ve 1–100. satırlar hiç çekilmez |
| Takvim sayfalaması | Durma koşulu **boş sayfa** | Tutarlı sorguda `len < limit` de geçerli bir koşuldur (`splits`: 98 → sonraki sayfa boş); boş sayfa kuralı tek fazladan istekle daha muhafazakâr |
| Takvim tablolarında sembol FK'sı | **Yok**, `is_known` bayrağı var | Kaynakta evren dışı semboller geliyor; `news_symbols` ile aynı gerekçe |
| PK'ya giren serbest metinler | `ascii_bin` / `utf8mb4_0900_as_cs` | Varsayılan `utf8mb4_0900_ai_ci` `Enflasyon` = `Enflâsyon` = `ENFLASYON` sayar (§4.4/M4) |
| `financialCurrency` kaynağı | `info`, best-effort | THYAO.IS tabloları **USD**, fiyatları TRY. `info` üç wire isteğidir; hata verirse `currency=NULL`, hücre `failed` **olmaz** |

---

## 3. Ortam

Önceki spec'le aynı: MySQL 8.3.0 (Docker, `localhost:3306`, şema `yfinance`,
test şeması `yfinance_test`, `sql_mode=STRICT_TRANS_TABLES`), Python 3.13.5,
yfinance 1.7.0, pandas 3.0.5, numpy 2.5.2.

Tüm §4 bulguları 2026-09-04 tarihinde canlı Yahoo çağrıları, yfinance kaynak
kodu okuması ve gerçek MySQL 8.3 deneyleriyle üretildi.

---

## 4. Keşif bulguları

Keşif matrisi: **10 sembol** (AAPL, MSFT, THYAO.IS, SPY, BTC-USD, VFIAX,
JPM, AIG, XOM, O, SAP.DE, 7203.T dâhil) × 14 çağrı; 4 takvim ucu × sayfalar;
8 piyasa bölgesi × 2 uç; ~15 MySQL deneyi.

### 4.1 Doğrulanmış davranışlar

| Bulgu | Gözlem | Tasarıma etkisi |
|---|---|---|
| Tablo şekli | `DataFrame`; **index = kalem etiketi (`str` dtype)**, **kolonlar = dönem sonu (`Timestamp`, tz-naive)** | Uzun şemaya çevirirken transpoze zorunlu |
| Kalem etiketi kaynağı | `const.fundamentals_keys` — **kapalı evren, 375 etiket**; `_get_financials_time_series` yalnızca `annual`/`quarterly` önekini siler ve kaynak listeye göre sıralar | `pretty=False` etiketleri kütüphane sürümüne bağlıdır, Yahoo HTML'ine değil |
| Kalem etiketi uzunluğu | AAPL income tek başına max 51; **3 sembol birleşiminde max 60** (`FinancialAssetsDesignatedasFairValueThroughProfitorLossTotal`, MSFT bilanço); kapalı evrende de max 60; `pretty=True` max 68 | `VARCHAR(128) ascii_bin` |
| Etiket karakter kümesi | 302 canlı ve 375 kapalı etiketin **tamamı** `isalnum()` | Ayırıcı kaçışı gerekmez |
| Kalem sayısı | 3 sembolde 237, 10 sembolde **302** farklı etiket | Geniş şema seçilseydi her sektörde migration |
| Kalem seti sembole göre değişir | AAPL income 39, MSFT 47, THYAO 51 satır | "Sabit kolon seti" varsayımı kurulamaz |
| Index tekilliği | `is_unique = True`, duplicate 0 | `item_key` PK'nın parçası olabilir |
| Değer tipi ve aralığı | Tüm kolonlar `float64`; ölçülen max mutlak **1,06×10¹⁴** (7203.T, JPY); aynı tabloda `TaxRateForCalcs = 0.156` | `DECIMAL(38,10)`: 28 tam + 10 ondalık |
| `NaN` yoğunluğu | AAPL income: 195 hücrenin **45'i** `NaN` | Seyrek matris |
| `freq` kabul edilen değerler | **Tam olarak** `{"yearly","quarterly","trailing"}`; `"annual"`, `"ttm"`, `"YEARLY"`, `None` → `ValueError`. Alias yok, case-sensitive | DB'deki `annual`/`ttm` adları **yalnızca şema tarafıdır**, çağrıya geçmez |
| Frekans başına ayrı istek | `Financials._{income,balance_sheet,cash_flow}_time_series` sözlükleri `freq` anahtarlı, **`Ticker` örneği başına** önbelleklenir | 8 statement = 8 wire isteği (ölçüldü) |
| Pencere sınırı | Ölçülen max kolon: yıllık 5, çeyreklik 7 | Watermark'a gerek yok (§7.4) |
| Mali yıl sonu | AAPL 09-30, MSFT 06-30, THYAO 12-31 | Dönem sonu takvim yılına sabitlenemez |
| `financialCurrency` | THYAO.IS: `financialCurrency=USD`, `currency=TRY`. **Yalnızca `info`'da var** (`fast_info`'da yok, `history_metadata` yalnızca `currency` veriyor) | `info` çağrısı zorunlu, best-effort |
| `info` maliyeti | **3 wire isteği**: 5 modüllük `quoteSummary` + `_fetch_additional_info` (`v7/finance/quote`) + `_fetch_complementary` | §7.5 bütçesi |
| TTM tek kolonlu | `ttm_income_stmt` AAPL 33×1 | `freq='ttm'` aynı şemaya sığar |
| `calendar` anahtarları | **Kaynak garantili tam 9 anahtar** (`quote.py:_fetch_calendar`); sembole göre eksik anahtar olur | Tüm kolonlar NULL kabul eder |
| `calendar['Earnings Date']` | Liste; kaynakta **uzunluk sınırı yok**, ölçülen 6 sembolde `len=1` | `_start`/`_end`/`_count`; `len>2` uyarı üretir |
| `earnings_dates` index | tz-aware; THYAO.IS, SAP.DE, 7203.T dâhil **hepsi `America/New_York`**. `base.py` yalnızca `EDT`/`EST` kısaltmalarını çözer | `ts_utc` + `tz_name`; bilinmeyen kısaltma → hücre `failed` |
| `earnings_dates` limit | `limit>100` → `ValueError: Yahoo caps limit at 100`. `limit` üst sınır değil **kova boyutudur** (`limit=26` → 50 satır) | `limit=100` |
| `sec_filings` içeriği | `list[dict]`, 7 anahtar; AAPL'de 80/80 filing'de hepsi mevcut | Tipli kolonlar + `raw_json` |
| `sec_filings.exhibits` | `dict[str,str]`; kaynakta listeden dict'e çevrilirken **tekrarlı `type` üzerine yazılır** (`quote.py:_fetch_sec_filings`) | `exhibit_count` = tekil ek tipi sayısı |
| `filing_id` ayrıştırma | `edgarUrl` içinde `(\d{10}-\d{2}-\d{6})` → **80/80 başarılı, 0 hata** | Accession no doğal anahtar |
| `epochDate` birimi | **Saniye** (`1788220800` → 2026-09-01) | `filed_ts_utc` |
| `economic_calendar` anahtarı | Index (`Event`) tekil değil (100 satırda 29 tekrar); `(Event, Event Time, Region)` **100/100 tekil**; `Event Time` dtype `datetime64[us, UTC]` | Üçlü PK |
| `Market.status` | 11 anahtar; `open`/`close` **`datetime` nesnesi**, `timezone` iç içe dict, `duration` liste | `raw_json` için `default=str` encoder |
| `Market.status` bölge desteği | **Yalnızca `US`** — `domain/market.py` `id != 'us'` uyuşmazlığını tespit edip `self._status = None` yapar. Deterministik, flaky değil | 7 bölgede `empty` |
| `Market.summary` | Bölgeye göre 1–6 board; quote sözlüğü **29–31 anahtar** (CXI'de 29) | Tipli kolonların hepsi NULL kabul eder |
| Board kodları | US: CBT/CME/CMX/CXI · EUROPE: CCY/FGI/GER/PAR · ASIA: ASX/CCY/DJI/HKG/OSA/SHH · GB: CCY/CMX/FGI · RATES: CBT/**CGI** · COMMODITIES: CMX/NYM · CURRENCIES: CCY · CRYPTO: CCC | `board_code VARCHAR(8)` |
| `Calendars(start,end)` | Metot argümanları ctor'u geçersiz kılar, ctor durumunu **mutasyona uğratmaz**; yalnızca biri verilirse `UserWarning` | Pencere metotta verilir |
| Boş takvim sayfası | `_cleanup_df` `if df.empty: return df` ile erken çıkar → `set_index`/`rename`/`to_datetime` **uygulanmaz**, ham kolon adları döner | Boşluk kontrolü kolon erişiminden **önce** |

### 4.2 Çürütülen varsayımlar

İlk taslakta yanlıştı; her biri ölçümle düzeltildi. **Son üç satır bu
dokümanın kendi ilk sürümündeki hatalardır.**

| Yanlış varsayım | Gerçek | Kanıt |
|---|---|---|
| `earnings` dataset'i veri döndürür | Kaynakta koşulsuz `return None` | `Fundamentals.earnings` property |
| Her tablonun TTM'i vardır | Bilanço `trailing` kabul etmez | `ValueError`, `fundamentals.py:80-82` |
| `earnings_dates` damgası tekildir | AAPL `2002-07-16 16:00` → **2 satır**, tek fark `Surprise(%)` (2.55 / 13.43); EPS alanlarının ikisi de `NaN` | `index.duplicated().sum() == 1` |
| `sec_filings` her sembolde `list` | ABD dışı ve fon/ETF'te **`{}` (dict)** — `quote.py:592`: `self._sec_filings = {} if f is None else f` | THYAO.IS, SPY, BTC-USD, VFIAX |
| `Market.status` tüm bölgelerde çalışır | Yalnızca `US` | 7 bölge `None` |
| Şirket olmayan sembolde çağrı hata verir | Sessizce `(0,0)` / `None` / `{}` | SPY, BTC-USD, VFIAX |
| **Sayfalama `len(page) < limit` ile bitmez** | **Bu iddia geri çekildi.** `offset=0` → 2 satır gözlemi Yahoo sayfalaması değil, `filter_most_active=True` varsayılanının **yalnızca `offset==0`'da** uygulanmasıydı. `filter_most_active=False` ile `offset=0` → 100, `offset=100` → 100, örtüşme 0 | `calendars.py`: `if filter_most_active and not offset:` |
| **Aynı `Ticker` ile `offset` sayfalanabilir** | `TickerBase._earnings_dates` **yalnızca `limit`'e anahtarlı** (`base.py:637`) → `offset=100` aynı DataFrame'i döndürür (`a is b` → `True`). Taze Ticker ile `offset=100` → 12 satır (1999–2002), `offset=200` → `None` | Ölçüldü |
| **Kalem etiketi max 51 karakter** | **60.** 51 yalnızca AAPL income'ın maksimumuydu; 3 sembol birleşiminde `FinancialAssetsDesignatedasFairValueThroughProfitorLossTotal` (MSFT) var | Ölçüldü |
| **Şirket olmayan sembolde çağrı sessizce boş döner** | Bu, yfinance'ın **varsayılan** davranışıdır. Proje `yf.config.debug.hide_exceptions = False` yaptığı için (gerçek hatalar yutulmasın diye) aynı durum **istisnaya** dönüşür: fundamentals uçlarında `curl_cffi HTTPError 404` (`"No fundamentals data found for symbol: SPY"`), `trailing` uçlarında `IndexError: positional indexers are out-of-bounds` | Uygulama sırasında canlı çalıştırmada ortaya çıktı: 4 sembollük ilk koşu 10 hücreyi `failed` işaretledi |

### 4.3 Çok pazarlılık matrisi

11 sembol-kapsamlı dataset × 6 sembol. **Hiçbiri exception fırlatmadı.**

| dataset | AAPL | MSFT | THYAO.IS | SPY | BTC-USD | VFIAX |
|---|---|---|---|---|---|---|
| income_stmt | 39×5 | 47×4 | 51×4 | 0×0 | 0×0 | 0×0 |
| quarterly_income_stmt | 33×5 | 47×5 | 50×6 | 0×0 | 0×0 | 0×0 |
| ttm_income_stmt | 33×1 | 47×1 | 51×1 | 0×0 | 0×0 | 0×0 |
| balance_sheet | 69×5 | 79×5 | 86×5 | 0×0 | 0×0 | 0×0 |
| quarterly_balance_sheet | 65×7 | 79×7 | 86×6 | 0×0 | 0×0 | 0×0 |
| cashflow | 53×5 | 59×5 | 52×5 | 0×0 | 0×0 | 0×0 |
| quarterly_cashflow | 46×6 | 60×7 | 46×6 | 0×0 | 0×0 | 0×0 |
| ttm_cashflow | 45×1 | 56×1 | 44×1 | 0×0 | 0×0 | 0×0 |
| calendar | 9 anahtar | 8 | 8 | `{}` | `{}` | `{}` |
| earnings_dates | 100×3 | 100×3 | 43×3 | `None` | `None` | `None` |
| sec_filings | 80 | 82 | `{}` | `{}` | `{}` | `{}` |

Piyasa dataset'leri (2026-09-04 penceresi): `market_status` yalnızca US;
`market_summary` 8 bölgenin hepsinde dolu; `earnings_calendar` filtresiz 100
satır/sayfa; `economic_calendar` 100; `ipo_calendar` 3; `splits_calendar` 98
(sonraki sayfa boş).

### 4.4 MySQL 8.3 deney bulguları

| # | Bulgu | Kanıt |
|---|---|---|
| M1 | `last_value` **rezerve kelime** | `CREATE TABLE t (last_value DECIMAL(28,12))` → `ERROR 1064`; `information_schema.KEYWORDS`: `LAST_VALUE RESERVED=1` |
| M2 | ENUM listeleri ebeveyn/çocukta ayrışırsa FK **ordinal** üzerinden bağlar, `CREATE` uyarı vermez | Farklı sıralı ENUM ile çocuk satır `'balance_sheet'` yazılıp ebeveynin `'income'` satırına bağlandı; `statement+0` ordinal'i gösteriyor |
| M3 | ENUM'a **sona** değer eklemek `ALGORITHM=INSTANT` (11 ms); araya eklemek `ERROR 1846` + tablo yeniden kurulumu | Ölçüldü |
| M4 | Varsayılan `utf8mb4_0900_ai_ci` PK'da `Enflasyon` = `Enflâsyon` = `ENFLASYON` | `ERROR 1062 Duplicate entry` |
| M5 | `TEXT` kolon PK'ya giremez | `ERROR 1170 ... used in key specification without a key length` |
| M6 | `DECIMAL(38,10)` 11. basamağı **sessizce yuvarlar** (yalnızca `Note 1265 Data truncated`, hata değil) | `1.00000000005` → `1.0000000001` |
| M7 | Bileşik FK + ENUM + `ascii_bin` çalışıyor; InnoDB **ek indeks açmadı** (FK kolonları PK'nın ön eki) | `information_schema.STATISTICS` |
| M8 | 5 kolonlu PK `item_key VARCHAR(64)` ile **103 byte** = 3072 sınırının %3,4'ü; `VARCHAR(128)` ile 167 byte (%5,4) | `EXPLAIN key_len=105`; `item_key` utf8mb4×64 = 256 byte ile de sığdı |
| M9 | `financial_facts`'te `WHERE period_end=?` **full scan** (600 000 satır, 148 ms) | `EXPLAIN type=ALL rows=584920` |
| M10 | `_verify`'ın `OR` blokları çalışıyor ama `tuple_(...).in_(...)` **4,4× hızlı** (112 ms → 25 ms) ve `range_optimizer_max_mem_size`'a bağımlı değil | Ölçüldü |
| M11 | `ALTER TABLE sync_runs ADD COLUMN scope ... DEFAULT 'symbols'` 50 000 satırda `ALGORITHM=INSTANT`, 16 ms | Ölçüldü |
| M12 | PK'nın tüm parçaları NOT NULL olmak zorunda; UNIQUE'te NULL tekrarı serbest | `ERROR 1171` / 3 satır birden yazıldı |
| M13 | Hacim: 600 000 fact satırı = 75,8 MB veri + 20,6 MB indeks | `information_schema.TABLES` |

---

## 5. Veri modeli

Motor InnoDB, `utf8mb4` / `utf8mb4_0900_ai_ci`. Mevcut collation
istisnaları (`SymbolType()` = `VARCHAR(32) ascii ascii_bin`, `HashType()` =
`VARCHAR(64) ascii ascii_general_ci`) aynen geçerlidir. Yeni tablolar için üç
yeni tip fabrikası eklenir:

```python
def ShortHashType()  -> VARCHAR(16, charset="ascii", collation="ascii_bin")
def RegionType()     -> VARCHAR(16, charset="ascii", collation="ascii_bin")
def KeyTextType(n)   -> VARCHAR(n, charset="utf8mb4", collation="utf8mb4_0900_as_cs")
```

`ShortHashType` `ascii_bin`'dir: `CHAR(16) ascii_general_ci` hem büyük/küçük
harf ayrımını yutar hem `CHAR`'ın pad-space semantiğiyle sondaki boşluğu
kırpar (§4.4/M4). `KeyTextType` PK'ya giren **her** serbest metin için
zorunludur — varsayılan collation aksanı ve harf büyüklüğünü yok sayar.

### 5.1 ENUM'ların tek kaynağı

```python
class StatementKind(enum.StrEnum):
    INCOME = "income"; BALANCE_SHEET = "balance_sheet"; CASH_FLOW = "cash_flow"

class StatementFreq(enum.StrEnum):
    ANNUAL = "annual"; QUARTERLY = "quarterly"; TTM = "ttm"

STATEMENT_ENUM = Enum(StatementKind, values_callable=..., name="statement_kind")
FREQ_ENUM      = Enum(StatementFreq, values_callable=..., name="statement_freq")
```

`financial_periods` ve `financial_facts` **aynı `Enum` nesnesini** paylaşır.
İki ayrı ENUM tanımı değer sırası bir kez ayrışırsa FK'yı ordinal üzerinden
sessizce yanlış satıra bağlar (§4.4/M2) — MySQL bunu ne `CREATE`'te ne
`INSERT`'te bildirir. ENUM'a değer eklemek **yalnızca sona** yapılır
(§4.4/M3).

Bu ad tercihi yfinance'ın `freq` parametresiyle **kasıtlı olarak farklıdır**:
API `"yearly"`/`"trailing"` ister (§4.1), şema `annual`/`ttm` saklar.
Dönüşüm `StatementDataset` içinde tek yerdedir.

### 5.2 Finansal tablolar

**`financial_periods`** — PK (`symbol`, `statement`, `freq`, `period_end`)

| Kolon | Tip | Null | Not |
|---|---|---|---|
| `symbol` | `SymbolType()` | NOT NULL | FK → `symbols.symbol`, `ON UPDATE CASCADE ON DELETE RESTRICT` |
| `statement` | `STATEMENT_ENUM` | NOT NULL | §5.1 |
| `freq` | `FREQ_ENUM` | NOT NULL | §5.1 |
| `period_end` | `DATE` | NOT NULL | TTM'de "trailing as-of" tarihi |
| `currency` | `VARCHAR(8) ascii` | NULL | `info.financialCurrency`, best-effort |
| `item_count` | `INT UNSIGNED` | NOT NULL | **Yazılan** (NaN olmayan, uzunluk sınırını geçen) kalem sayısı |
| `raw_json` | `LONGTEXT` | NOT NULL | Dönemin tüm kalemleri; `NaN` → `null` |
| `content_hash` | `HashType()` | NOT NULL | Kanonik JSON'un SHA-256'sı |
| `fetched_at` | `DATETIME(6)` | NOT NULL | **Son doğrulama zamanı** (§7.3) |

`(statement, freq)` ikilisi PK'da olduğu için `quarterly_income_stmt` ile
`ttm_income_stmt`'in aynı `period_end`'i (AAPL: `2026-06-30`) paylaşması
çakışma yaratmaz.

**`financial_facts`** — PK (`symbol`, `statement`, `freq`, `period_end`, `item_key`)

| Kolon | Tip | Null | Not |
|---|---|---|---|
| `symbol`, `statement`, `freq`, `period_end` | ebeveynle **birebir aynı** tipler | NOT NULL | Bileşik FK → `financial_periods`, `ON UPDATE CASCADE ON DELETE CASCADE` |
| `item_key` | `VARCHAR(128) ascii_bin` | NOT NULL | Ölçülen max 60 |
| `value` | `DECIMAL(38,10)` | NOT NULL | `NaN` satırı yazılmadığı için NOT NULL |

`symbols`'a doğrudan FK yoktur — kısıt ebeveyn üzerinden geçer ve
`ON DELETE RESTRICT` orada zorlanır. PK boyutu 32+1 (`symbol`) + 1 + 1
(ENUM'lar) + 3 (`DATE`) + 128+1 (`item_key`) = **167 byte**, InnoDB'nin
3072 byte sınırının %5,4'ü; ölçüm `VARCHAR(64)` ile 103 byte yapılmıştı
(§4.4/M8), genişletme sınırı zorlamıyor. FK kolonları PK'nın ön eki olduğu
için InnoDB ek indeks açmaz (§4.4/M7).

`mode="replace_scope"`, `scope_columns=("symbol","statement","freq","period_end")`.
Bir dönemin tüm kalemleri `NaN` gelirse `rows` boş olur; bu durumda da silme
yapılabilmesi için `TableWrite.scope_values` zorunludur (§6.2).

**Hacim:** sembol başına ≈1 500 satır; 1 000 sembolde 1,5 M satır ≈ **240 MB
veri + indeks** (§4.4/M13). `financial_periods.raw_json` ayrıca ~36 000 satır
× birkaç KB ≈ 100 MB ekler.

### 5.3 Sembol-kapsamlı takvim ve dosyalama tabloları

**`ticker_calendar`** — PK `symbol` · **`ticker_calendar_history`** — PK (`symbol`, `fetched_at`)

Ortak kolonlar: `dividend_date` DATE, `ex_dividend_date` DATE,
`earnings_date_start` DATE, `earnings_date_end` DATE,
`earnings_date_count` TINYINT UNSIGNED NOT NULL DEFAULT 0,
`earnings_high`/`earnings_low`/`earnings_average` `DECIMAL(28,12)`,
`revenue_high`/`revenue_low`/`revenue_average` `DECIMAL(38,0)`,
`raw_json` NOT NULL, `content_hash` NOT NULL, `fetched_at` NOT NULL.
Tarih ve tutar kolonlarının **hepsi NULL kabul eder**.

`Earnings Date` listesinin uzunluğunda kaynakta sınır yoktur; `len > 2`
görülürse `_start=[0]`, `_end=[-1]`, `_count=len` yazılır ve
`WARNING: earnings date list has N entries` loglanır (ara elemanlar kaybolur).

**`earnings_dates`** — PK (`symbol`, `earnings_ts_utc`, `fact_hash`)

`symbol` FK'lı; `earnings_ts_utc` `DATETIME(6)` NOT NULL;
`fact_hash` `ShortHashType()` NOT NULL — `(eps_estimate, reported_eps,
surprise_pct)` üçlüsünün kanonik JSON'unun (`allow_nan=False`, yani
`NaN → null`) SHA-256'sının ilk 16 hanesi; `earnings_date_local` DATE NOT NULL;
`tz_name` `VARCHAR(64)` NOT NULL; `eps_estimate`/`reported_eps`/`surprise_pct`
`DECIMAL(28,12)` NULL; `fetched_at` NOT NULL.

`fact_hash` zorunludur: AAPL `2002-07-16 16:00` damgasında iki satır var ve
**tek farkları `Surprise(%)`** (2.55 / 13.43); EPS alanlarının ikisi de
`NaN`. `allow_nan=False` dönüşümü yapılmazsa iki satır aynı hash'i alır ve
biri sessizce kaybolur.

**`sec_filings`** — PK (`symbol`, `filing_id`)

`filing_id` `VARCHAR(64) ascii_bin`: `edgarUrl` içinden
`(\d{10}-\d{2}-\d{6})` (80/80 başarılı), bulunamazsa
`sha256(f"{date}|{type}|{title}")[:32]`. Diğer kolonlar: `filing_date` DATE
NOT NULL, `filed_ts_utc` `DATETIME(6)` NOT NULL (`epochDate`, saniye),
`filing_type` `VARCHAR(32) ascii_bin` NOT NULL, `title` TEXT, `edgar_url`
TEXT, `exhibit_count` `SMALLINT UNSIGNED` NOT NULL, `raw_json` NOT NULL,
`fetched_at` NOT NULL.

`exhibit_count` **tekil ek tipi sayısıdır**: yfinance ek listesini
`{type: url}` sözlüğüne çevirirken aynı tipteki ikinci eki üzerine yazar
(§4.1). NOT NULL alanlardan biri kaynakta eksikse o dosyalama **atlanır** ve
`WARNING: sec filing missing required field` loglanır — aksi hâlde
`ERROR 1048` sembolün tüm transaction'ını düşürürdü.

**`sec_filing_exhibits`** — PK (`symbol`, `filing_id`, `exhibit_type`, `url_hash`)

`exhibit_type` `VARCHAR(32) ascii_bin`, `url` TEXT NOT NULL,
`url_hash` `ShortHashType()` NOT NULL = `sha256(url)[:16]`.
`url` PK'ya giremez (§4.4/M5); `url_hash` olmadan aynı dosyalamadaki ikinci
`EX-99.1` eki `ERROR 1062` verir ve `rows_verified < rows_attempted` ile run
`partial` olur. Bileşik FK → `sec_filings`, `ON DELETE CASCADE`.
`mode="replace_scope"`, `scope_columns=("symbol","filing_id")`.

### 5.4 Piyasa tabloları

Hiçbirinde sembol FK'sı yoktur. `region` kolonu her yerde `RegionType()`.

**`market_status`** — PK `region` · **`market_status_history`** — PK (`region`, `fetched_at`)
`market_id` `VARCHAR(32)`, `name` `VARCHAR(64)`, `status` `VARCHAR(32)`,
`yfit_market_status` `VARCHAR(64)`, `message` TEXT,
`open_ts_utc`/`close_ts_utc` `DATETIME(6)`, `timezone_name` `VARCHAR(64)`,
`gmt_offset` INT, `tz_short` `VARCHAR(16)`, `raw_json`, `content_hash`,
`fetched_at`. Tipli kolonların hepsi NULL kabul eder.

**`market_summary`** — PK (`region`, `board_code`) · **`market_summary_history`** — PK (`region`, `board_code`, `fetched_at`)
`board_code` `VARCHAR(8) ascii_bin`; `symbol` `SymbolType()` NULL (**FK yok**),
`is_known` BOOL NOT NULL; `short_name` `VARCHAR(64)`, `quote_type`
`VARCHAR(32)`, `exchange` `VARCHAR(32)`, `market_state` `VARCHAR(16)`,
`currency` `VARCHAR(8)`, `regular_market_price`/`regular_market_change`/
`regular_market_change_percent`/`regular_market_previous_close`
`DECIMAL(28,12)`, `regular_market_ts_utc` `DATETIME(6)`,
`exchange_timezone_name` `VARCHAR(64)`, `raw_json`, `content_hash`,
`fetched_at`. Quote sözlüğü 29–31 anahtardır; **tipli kolonların hepsi NULL
kabul eder** (CXI'de iki anahtar eksik), kalanlar `raw_json`'da.

**`calendar_earnings`** — PK (`symbol`, `event_start_ts_utc`)
`company` `VARCHAR(255)`, `market_cap` `DECIMAL(38,4)`, `event_name`
`KeyTextType(64)`, `timing` `VARCHAR(8)`, `eps_estimate`/`reported_eps`/
`surprise_pct` `DECIMAL(28,12)`, `is_known` BOOL NOT NULL, `fetched_at`.

**`calendar_economic`** — PK (`region`, `event_time_utc`, `event_name`)
`event_name` `KeyTextType(64)` (ölçülen max 27), `period_for` `VARCHAR(16)`,
`actual`/`expected`/**`last_reported`**/`revised` `DECIMAL(28,12)` NULL,
`fetched_at`. Kolon adı `last_value` **olamaz** — MySQL 8 rezerve kelimesi
(§4.4/M1).

**`calendar_ipo`** — PK (`symbol`, `ipo_date_utc`, `action`)
`action` `VARCHAR(16) ascii_bin`, `company` `VARCHAR(255)`, `exchange`
`VARCHAR(32)`, `filing_date` DATE NULL, `amended_date` DATE NULL,
`price_from`/`price_to`/`price` `DECIMAL(28,12)` NULL, `currency`
`VARCHAR(8)`, `shares` `DECIMAL(38,0)` NULL, `is_known`, `fetched_at`.
Ölçümde `Filing Date`, `Amended Date`, `Price*` ve `Shares` 3/3 satırda boş
geldi.

**`calendar_splits`** — PK (`symbol`, `payable_on_utc`)
`company` `VARCHAR(255)`, `optionable` BOOL, `old_share_worth` INT,
`share_worth` INT, `ratio` `DECIMAL(28,12)` NULL (Python'da
`share_worth / old_share_worth`; payda 0 veya NULL ise NULL), `is_known`,
`fetched_at`.

**PK bileşenleri NOT NULL'dır** (§4.4/M12). Kaynakta `symbol`,
`*_ts_utc`/`*_time_utc` ya da `action` boş/`NaT` gelen takvim satırı
**yazılmaz**; `WARNING: calendar row missing key` loglanır ve satır
`rows_attempted`'a girmez.

### 5.5 `sync_runs` değişikliği

`sync_runs`'a `scope` `ENUM('symbols','market')` NOT NULL DEFAULT `'symbols'`
kolonu eklenir; 50 000 satırlık tabloda `ALGORITHM=INSTANT`, 16 ms
(§4.4/M11). `sync_run_items.symbol` alanına bölge kodu (`US`) ya da bölgesiz
dataset'lerde `*` yazılır — bu kolonda FK yoktur.

### 5.6 Tip kararları

- **`DECIMAL(38,10)` yalnızca `financial_facts` için.** 28 tam basamak
  ölçülen max değerin (1,06×10¹⁴) çok üstünde. MySQL 11. ve sonraki
  basamakları **sessizce yuvarlar** (yalnızca `Note 1265`, §4.4/M6), bu
  yüzden değer yazılmadan önce Python'da `Decimal(repr(float(x)))
  .quantize(Decimal("1E-10"))` ile **bilinçli** olarak yuvarlanır.
  `raw_json` ham float'ı korur; `raw_json` ile `financial_facts`'i
  karşılaştıran her testin toleransı `1e-10`'dur.
- **Fiyat/oran tabloları `DECIMAL(28,12)` kalır.** İki tip bilinçli olarak
  farklıdır: finansal kalemler oran ve mutlak tutarı aynı kolonda karıştırır.
- **Tüm damgalar `DATETIME(6)`.** `_history` PK'ları `fetched_at` içerir;
  `DATETIME(0)` aynı saniyede `ERROR 1062` verir. `DATETIME` oturum
  `time_zone` değişiminden etkilenmez (ölçüldü).
- **`raw_json` `LONGTEXT`.** `Market.status` gövdesi `datetime` nesneleri
  taşır ve düz `json.dumps` `TypeError: Object of type datetime is not JSON
  serializable` verir; `HistoryMetadata` ile aynı `default=str` encoder
  kullanılır.
- **Satır boyutu.** Ölçülen en geniş yeni tablo `market_summary` = 1 857 byte
  (utf8mb4 en kötü hâl), sınırın çok altında.

### 5.7 İndeksler

Yalnızca **PK'nın ön eki olmayan** erişim yolları indekslenir. Ölçümle
gereksiz bulunan indeksler (`earnings_dates (symbol, ts DESC)`,
`ticker_calendar_history (symbol, fetched_at DESC)`,
`market_status_history (region, fetched_at DESC)`,
`market_summary_history (region, board_code, fetched_at DESC)`, takvim
tablolarındaki `(symbol)`) **açılmaz**: InnoDB kümelenmiş indeksi geriye
doğru da tarar ve bunların hepsi ilgili PK'nın ön ekidir.

- `financial_periods (period_end)`
- `financial_facts (period_end, item_key)` — `WHERE period_end=?` aksi hâlde
  full scan yapar (600 000 satırda 148 ms ölçüldü, §4.4/M9)
- `financial_facts (item_key, period_end)` — "tüm sembollerde TotalRevenue"
- `sec_filings (symbol, filing_date DESC)`, `sec_filings (filing_type)`
- `market_summary (symbol)`, `market_summary_history (symbol)` — `symbol`
  PK'da değil ve FK yok, InnoDB kendiliğinden açmaz
- `calendar_earnings (event_start_ts_utc)`, `calendar_economic (event_time_utc)`,
  `calendar_ipo (ipo_date_utc)`, `calendar_splits (payable_on_utc)`

### 5.8 View

Yeni view eklenmez (YAGNI). Pivot ihtiyacı sorgu tarafında çözülür.

---

## 6. Mimari

Bu bölüm **mevcut kodun gerçek arayüzlerine** göre yazılmıştır:
`src/yfin/persistence.py` (`RowWriter` protokolü, `MySQLRowWriter`,
`apply_write`), `src/yfin/datasets/base.py` (`Dataset[RawT]`,
`upsert(writer, result)`), `src/yfin/datasets/snapshot_base.py`.
Dataset'ler `Session` görmez; bu katman ayrımı korunur.

### 6.1 Registry generikleştirmesi

Alias genişletme, sıra koruyan tekilleştirme, topolojik sıralama, döngü ve
bilinmeyen-ad kontrolü `Registry` sınıfına taşınır:

```python
class Registry[D]:
    def __init__(self, *, bootstrap: str | None = None,
                 aliases: Mapping[str, tuple[str, ...]] | None = None) -> None: ...
    def register(self, ds: D) -> D: ...
    def names(self) -> list[str]: ...
    def user_visible_names(self) -> list[str]: ...
    def resolve(self, names: Sequence[str] | None) -> list[D]: ...
    def unregister(self, name: str) -> None: ...      # yalnızca test
    def __contains__(self, name: str) -> bool: ...
    def __getitem__(self, name: str) -> D: ...
    def __len__(self) -> int: ...

SYMBOL_DATASETS: Registry[Dataset[Any]] = Registry(
    bootstrap="symbols",
    aliases={
        "actions": ("dividends", "splits", "capital_gains"),
        "financials": ("income_stmt", "quarterly_income_stmt", "ttm_income_stmt",
                       "balance_sheet", "quarterly_balance_sheet",
                       "cashflow", "quarterly_cashflow", "ttm_cashflow"),
        # Ek B: AYRI alias, `financials`in parcasi DEGIL (B.6)
        "valuation": ("valuation_measures", "quarterly_valuation_measures"),
    },
)
MARKET_DATASETS: Registry[GlobalDataset[Any]] = Registry(
    bootstrap=None,
    aliases={"calendars": ("earnings_calendar", "economic_calendar",
                           "ipo_calendar", "splits_calendar"),
             "market": ("market_status", "market_summary")},
)
```

**`Dataset` generic'tir** (`class Dataset[RawT]`); `mypy --strict`
`disallow_any_generics` nedeniyle çıplak `Dataset` yazılamaz — bu yüzden
`Registry[Dataset[Any]]` (mevcut kodun `registry.py:10`'daki deseni).

**`bootstrap=None` semantiği açıkça tanımlıdır:** `wanted` sözlüğü boş başlar,
`visit()` yalnızca seçili adlar için çağrılır. Mevcut kod bu dalda çöker
(`wanted = {None: None}` → `visit(None)` → `UnknownDatasetError`), dolayısıyla
bu **yeni ve test edilmesi zorunlu** bir daldır. `names=None` ile `names=[]`
eşdeğerdir (hepsi); `user_visible_names()` bootstrap yokken tüm kayıtları +
alias'ları döner.

**Çağrı yerleri** — modül düzeyindeki `REGISTRY`/`resolve` kaldırıldığı için
üç dosya değişir: `cli.py`, **`runner.py`** (`runner.py:20` import,
`runner.py:230` `REGISTRY.get(dataset_name)`) ve
**`datasets/__init__.py`** (yeniden ihraç + `__all__`).
`runner._failed_records` registry'yi parametre olarak alır; böylece
`market_runner` aynı fonksiyonu `MARKET_DATASETS` ile çağırabilir.

`tests/unit/test_registry.py` **yeniden yazılır**: sabit sayı iddiaları
(sabit sayı iddiaları KALDIRILIR — registry o tarihten sonra AH ve Ek B ile
büyüdü; bugün 40 kayıt / 46 kullanıcı-görünür ad) güncellenir ve
döngü testindeki sözlük mutasyonu (`REGISTRY["_cycle_a"] = …` / `del`)
`register` + `unregister` ile değiştirilir. §10/1 adımı "davranış değişmez"
değil, "**davranış korunur, testler güncellenir**" biçimindedir.

### 6.2 `TableWrite` genişlemesi ve `MySQLRowWriter` düzeltmesi

```python
@dataclass(frozen=True)
class TableWrite:
    table: str
    rows: list[dict[str, Any]]
    key_columns: tuple[str, ...]
    update_columns: tuple[str, ...]
    mode: WriteMode = "upsert"
    scope_columns: tuple[str, ...] = ("symbol",)                 # YENİ
    scope_values: tuple[Mapping[str, Any], ...] | None = None    # YENİ
```

`MySQLRowWriter.write` (`persistence.py:76-105`) iki noktada düzeltilir:

1. **Erken çıkış silmeden önce.** Bugün `if not write.rows: return 0` satırı
   `replace_scope` kolundan **önce** (`persistence.py:78`). Bir dönemin tüm
   kalemleri `NaN` geldiğinde eski `financial_facts` satırları kalıcı olarak
   kalır ve §9.3'ün `COUNT(facts) == SUM(item_count)` invaryantı kırılır.
   Yeni sıra: `replace_scope` silmesi → `rows` boşsa `return 0`.
2. **Kapsam `scope_columns`'tan kurulur**, `table.c["symbol"]` sabit kodundan
   değil. Kapsam değerleri `scope_values` verilmişse ondan, verilmemişse
   `rows`'tan türetilir. Varsayılan `("symbol",)` sayesinde
   `company_officers` dâhil mevcut çağrılar değişmeden çalışır.

`MySQLRowWriter._verify`'ın çok kolonlu dalı `tuple_(*cols).in_([...])`
biçimine geçirilir: aynı erişim planı (`type=range, key=PRIMARY`), **4,4×
daha hızlı** ve 3,3× daha küçük SQL (§4.4/M10). `VERIFY_CHUNK = 500` korunur;
yorumuna `range_optimizer_max_mem_size` bağımlılığı yazılır.

### 6.3 Hash kapısı: iki farklı desen

Mevcut `RowWriter.current_hash(table, symbol)` anahtarı `symbol` olarak sabit
kodlar (`persistence.py:137`). Protokol genelleştirilir:

```python
def current_hash(self, table: str, key: Mapping[str, Any]) -> str | None: ...
```

Bunun üzerine **iki ayrı desen** kurulur — tek desen yetmez:

**(a) `SnapshotDataset` (mevcut, genelleştirilir).** Karşılaştırma
`snapshot_table`'da, yazma `history_table`'a yapılır; iki tablo farklı
olduğu için "önce karşılaştır, sonra yaz" sırası güvenlidir.
`key_columns: tuple[str, ...] = ("symbol",)` sınıf niteliği eklenir ve
`snapshot_base.py:41`'deki `row["symbol"]` yerine
`{c: row[c] for c in self.key_columns}` kullanılır. Böylece
`ticker_calendar` (`symbol`), `market_status` (`region`) ve `market_summary`
(`region`, `board_code`) aynı makineyi paylaşır.

**(b) `HashGatedDataset` (yeni).** `financial_periods` bu deseni
**kullanamaz**: karşılaştırılan tablo ile yazılan tablo **aynıdır**, ve
atlanacak olan `_history` değil **çocuk tablo** `financial_facts`'tir.
Kural:

1. `current_hash("financial_periods", key)` sorgusu **her şeyden önce**.
2. Hash eşitse `financial_facts` `TableWrite`'ı **hiç üretilmez**;
   `skipped["financial_facts"] += item_count`.
3. `financial_periods` satırı **her durumda** yazılır. Hash eşitse
   `update_columns = ("fetched_at",)`, değilse tüm kolonlar.

Böylece `fetched_at` "son doğrulama zamanı"dır, "son değişiklik zamanı"
değil; ve dönem hiç değişmese bile başlık satırı bayatlamaz.

### 6.4 `GlobalDataset` ve `MarketContext`

```python
@dataclass(frozen=True)
class MarketContext:
    fetched_at: datetime
    start: date
    end: date
    region: str | None = None          # yalnızca scope="region" dataset'lerinde dolu
    def cached(self, key: str, fn: Callable[[], Any]) -> Any: ...

class GlobalDataset[RawT](ABC):
    name: str
    produces: tuple[str, ...] = ()
    scope: Literal["global", "region"] = "global"

    @abstractmethod
    def fetch(self, mctx: MarketContext) -> RawT: ...
    @abstractmethod
    def normalize(self, raw: RawT) -> NormalizedResult: ...
    def upsert(self, writer: RowWriter, result: NormalizedResult) -> WriteStats: ...
```

`normalize` `symbol` almaz; tek fark budur. `TableWrite`,
`NormalizedResult`, `WriteStats`, `apply_write`, `RowWriter` ve
`SnapshotDataset` paylaşılır. `MarketContext` `SyncContext`'ten **türemez** —
ortak taban, `symbol` alanının piyasa tarafında var sanılmasına yol açardı.

**Bölge döngüsü dataset'in dışındadır.** `scope="region"` olan dataset için
`market_runner` her bölgeyi ayrı bir `fetch → normalize → upsert` turu olarak
işler ve `MarketContext.region`'ı doldurur. Bunun üç sonucu var: (1)
`sync_run_items` granülerliği doğal olarak **(dataset × tablo × bölge)**
olur; (2) `WriteStats`'in bölge kırılımı taşıması gerekmez; (3) mevcut
`_record_items` bir `DatasetMeta` protokolüne (`name`, `produces`) göre
yeniden tiplendirilerek iki runner arasında paylaşılır.

`Market` nesnesi bölge başına `mctx.cached(f"market:{region}", …)` ile bir
kez kurulur; `.status` ve `.summary` aynı örnekten okunur. `domain/market.py`
US dışı bölgelerde `_status = None` bıraktığı için `_parse_data`'nın kısa
devre guard'ı hiç çalışmaz; bugün ikinci isteği `YfData` LRU önbelleği
(`cache_maxsize=64`) emiyor, ama bu tesadüftür — tek örnek kuralı bunu
tesadüfe bırakmaz.

`Calendars` örneği `mctx.cached("calendars", …)` ile dört takvim dataset'i
arasında paylaşılır. Aynı `(tip, limit, offset, pencere)` ikinci kez
istenirse yfinance **ağa çıkmadan eski sonucu döndürür**; retry gerekiyorsa
`force=True` verilir.

### 6.5 Kayıtlı dataset'ler

**Sembol-kapsamlı (`SYMBOL_DATASETS`), 11 yeni kayıt**, hepsinde
`depends_on = ("symbols",)`:

| `name` | yfinance çağrısı | `produces` |
|---|---|---|
| `income_stmt` | `get_income_stmt(pretty=False, freq="yearly")` | `financial_periods`, `financial_facts` |
| `quarterly_income_stmt` | `… freq="quarterly"` | aynı |
| `ttm_income_stmt` | `… freq="trailing"` | aynı |
| `balance_sheet` | `get_balance_sheet(pretty=False, freq="yearly")` | aynı |
| `quarterly_balance_sheet` | `… freq="quarterly"` | aynı |
| `cashflow` | `get_cashflow(pretty=False, freq="yearly")` | aynı |
| `quarterly_cashflow` | `… freq="quarterly"` | aynı |
| `ttm_cashflow` | `… freq="trailing"` | aynı |
| `calendar` | `get_calendar()` | `ticker_calendar`, `ticker_calendar_history` |
| `earnings_dates` | sayfa başına **taze `yf.Ticker`** ile `get_earnings_dates(limit=100, offset=N)` | `earnings_dates` |
| `sec_filings` | `get_sec_filings()` | `sec_filings`, `sec_filing_exhibits` |

İlk sekizi tek bir `StatementDataset` sınıfının parametreli örnekleridir
(`statement`, `freq`, `name`). Ayrı kayıt olmalarının nedeni **hata
izolasyonu**: her `(tablo, frekans)` ayrı bir HTTP isteğidir.

**Piyasa-kapsamlı (`MARKET_DATASETS`), 6 kayıt:**

| `name` | `scope` | yfinance çağrısı | `produces` |
|---|---|---|---|
| `market_status` | region | `Market(region).status` | `market_status`, `market_status_history` |
| `market_summary` | region | `Market(region).summary` | `market_summary`, `market_summary_history` |
| `earnings_calendar` | global | `get_earnings_calendar(limit=100, offset=N, filter_most_active=False)` | `calendar_earnings` |
| `economic_calendar` | global | `get_economic_events_calendar(limit=100, offset=N)` | `calendar_economic` |
| `ipo_calendar` | global | `get_ipo_info_calendar(limit=100, offset=N)` | `calendar_ipo` |
| `splits_calendar` | global | `get_splits_calendar(limit=100, offset=N)` | `calendar_splits` |

**Zorunlu notlar:**

- **`pretty=False` zorunludur.** `pretty=True` boşluklu sunum biçimi üretir;
  anahtar olarak kullanılırsa Yahoo boşluk yerleşimini değiştirdiğinde tüm
  kalemler duplike olur.
- **`earnings_dates` her sayfa için yeni `Ticker` ister.**
  `TickerBase._earnings_dates` sözlüğü yalnızca `limit`'e anahtarlıdır
  (`base.py:637`); aynı Ticker'da `offset` **yok sayılır** ve sayfalama
  sessizce no-op olur. Ölçüm: taze Ticker ile `offset=100` → 12 satır
  (1999–2002), `offset=200` → `None`. Bu, `ctx.cached` ilkesinin
  `news`'ten sonra **ikinci istisnasıdır**.
- **`earnings_calendar` `filter_most_active=False` ister.** Varsayılan `True`
  yalnızca `offset==0`'da uygulanır (`calendars.py`); filtresiz sayfa 1+ ile
  birleştirilirse evrenin 1–100. satırları hiç çekilmez ve iki farklı sorgu
  tek tabloya karışır. Ayrıca varsayılan, ek bir `screen()` isteği doğurur.
- **`financialCurrency` best-effort'tur.** `ctx.cached("info", …)` hata
  verirse `currency=NULL` yazılır ve `WARNING: financialCurrency unavailable`
  loglanır; statement hücresi `failed` **olmaz**. `info` çağrısı üç wire
  isteğidir (§4.1).

---

## 7. Veri akışı

### 7.1 Sembol tarafı

Yeni 11 dataset mevcut hattın içine olduğu gibi girer: paralellik ekseni
sembol, worker `fetch`+`normalize`, ana thread sembol başına tek transaction,
`sync_run_items`'a tablo başına bir satır.

`runner.py`'de **tek bir değişiklik** vardır: `_failed_records`'ın registry
aramasını (`runner.py:230`) parametreye çevirmek ve `_record_items` /
`_failed_records`'ı `DatasetMeta` protokolüne göre tiplendirmek (§6.4).
Orkestrasyon mantığı — kuyruk, backpressure, transaction sınırı, exit code
türetmesi — değişmez.

### 7.2 Piyasa tarafı — `market_runner.py`

```
yfin market sync --datasets all
  │
  ├─ GET_LOCK('yfin_market_sync', 0) → alınamazsa çıkış 4
  │     (sembol sync'inin kilidi 'yfin_sync'; ikisi eşzamanlı koşabilir)
  │
  ├─ sync_runs: scope='market', symbol_count=0, dataset_count=len(datasets)
  │
  └─ TEK THREAD:
        global dataset  → 1 tur  (fetch → normalize → upsert → commit)
        region dataset  → bölge başına 1 tur, MarketContext.region dolu
        sync_run_items: (dataset × tablo × bölge), symbol = bölge | '*'
```

`symbol_count = 0` zorunludur: `RunSummary.exit_code()` kod 1'i
(`EXIT_NO_SYMBOL_RESOLVED`) yalnızca `symbol_count` doluysa üretir
(`runner.py:78`). Sıfır yazılmazsa `resolved_symbols` semantiği sessizce
"çözülen sembol"den "yazılan bölge"ye kayar ve `yfin status` yanıltıcı olur.
`yfin status` `scope='market'` satırlarında "semboller" yerine "bölgeler"
yazar.

Transaction sınırı **tur** düzeyindedir: `economic_calendar` patladığında
`splits_calendar` yazılmış kalır; `market_summary`'nin EUROPE turu
patladığında US turu kalır.

Paralellik yoktur ve gerekmez: 6 dataset, tipik ~25 istek.

### 7.3 Idempotency ve hash

- Tüm yazmalar `INSERT … ON DUPLICATE KEY UPDATE`, `update_columns` ile
  kapsamı sınırlı.
- `financial_periods.content_hash` kanonik JSON (`sort_keys=True`,
  `allow_nan=False`, `ensure_ascii=False`, `separators=(",",":")`) üzerinden
  SHA-256. Hash eşitse `financial_facts` yazılmaz (`skipped`), başlık satırı
  yalnızca `fetched_at` güncellenerek yazılır (§6.3/b).
- `ticker_calendar`, `market_status`, `market_summary` `SnapshotDataset`
  deseniyle çalışır (§6.3/a).
- `earnings_dates`, `sec_filings` ve takvim tabloları saf upsert'tir.
  Değişmeyen satır ikinci çalıştırmada `ROW_COUNT()=0` verir ama doğrulama
  anahtar varlığı sorgusuyla yapıldığı için hücre yine `ok` olur.

### 7.4 Pencere ve sayfalama

- **Statement dataset'lerinde watermark yoktur.** Yahoo en fazla 5 yıl / 7
  çeyrek döndürüyor (ölçüldü); "son dönemden itibaren" parametresi yok. Her
  çalıştırma tam pencereyi çeker, `content_hash` yazmayı ayıklar.
- **`earnings_dates`:** `limit=100`, `offset` 0'dan 100'er artar, **her sayfa
  için taze `Ticker`**. Durma koşulu: `None` ya da boş sayfa. Üst sınır
  `YF_EARNINGS_DATES_MAX_PAGES` (varsayılan 3; ölçümde 2. sayfa 12 satır, 3.
  sayfa `None`). Sayfalar birleştirilirken PK üzerinden tekilleştirilir
  (ölçülen örtüşme: 1 satır).
- **Takvim pencereleri:** `start = bugün - YF_CALENDAR_LOOKBACK_DAYS` (7),
  `end = bugün + YF_CALENDAR_LOOKAHEAD_DAYS` (30); `--start`/`--end` bunları
  geçersiz kılar. Sayfa boyutu 100, durma koşulu **boş sayfa**, üst sınır
  `YF_CALENDAR_MAX_PAGES` (varsayılan 5; ölçülen en büyük takvim tek sayfada
  100 satır).
- **Budama (retention) — varsayılan KAPALI.** Takvim tabloları ve
  `_history` anlık görüntüleri birikir. `yfin prune` iki tarih bayrağı
  kazanır: `--calendars-before` ve `--history-before`. İkisi de yalnızca
  `YF_PRUNE_ENABLED=true` (ya da `--force`) ile çalışır, aksi hâlde komut
  çıkış kodu 2 ile reddeder. Gerekçe: takvim uçları **pencere tabanlıdır**
  ("tüm geçmiş" diye bir uç yoktur) ve `_history` satırlarının kaynakta
  karşılığı yoktur — silinen satır geri getirilemez. `--dry-run` silmeden
  sayar. Öksüz haber temizliği bu kapının dışındadır ve varsayılan çalışır.
  `_history` tablo listesi elle tutulmaz, `Base.metadata`'dan türetilir
  (`_history` son eki + `fetched_at` kolonu), böylece yeni bir snapshot
  çifti kendiliğinden kapsanır.

### 7.5 İstek bütçesi ve rate limiting

Mevcut process-global token bucket (varsayılan 2 istek/sn) ve `tenacity`
backoff'u aynen kullanılır. Wire seviyesinde ölçülen maliyetler:

| Kapsam | İstek |
|---|---|
| 8 statement dataset'i | 8 |
| `calendar` | 1 |
| `earnings_dates` | 1 (ilk sayfa) + sayfa başına 1 |
| `sec_filings` | 1 |
| `info` (yalnız `financialCurrency` için gerekse bile) | **3** |
| **Sembol başına toplam** | **~14** (`info` zaten seçiliyse 11) |

2 istek/sn'de sembol başına ~7 sn; 100 sembolde **~12 dakika**. Bu,
`--datasets financials` ile finansal tabloları ayrı bir zamanlamaya almanın
somut gerekçesidir.

Piyasa sync'i: 8 bölge × (`status` + `summary`) = **16** + takvim sayfaları
(tipik 4, en kötü `4 × 5 = 20`) → tipik ~20, üst sınır ~36 istek.

---

## 8. Hata yönetimi ve veri bütünlüğü

### 8.1 İzolasyon sınırı

Sembol tarafında **(sembol × dataset)** hücresi, tablo başına bir
`sync_run_items` satırı. Piyasa tarafında sınır **(dataset × tablo ×
bölge)**'dir; `symbol` alanına bölge veya `*` yazılır. Durum kümesi ortaktır
(`ok`/`empty`/`skipped`/`failed`); `unknown_symbol` piyasa tarafında oluşmaz.

Çıkış kodları: `failed` yok → 0; kısmi → 2; tümü `failed` → 3; kilit
alınamadı → 4. Kod 1 yalnızca sembol sync'inde anlamlıdır ve piyasa
tarafında `symbol_count=0` sayesinde hiç tetiklenmez (§7.2).

### 8.2 `empty` ≠ `failed` — ölçülmüş boş durumlar

| Çağrı | Boş dönen | Boş biçimi |
|---|---|---|
| 8 statement dataset'i | SPY, BTC-USD, VFIAX | `DataFrame (0,0)` |
| `get_calendar` | SPY, BTC-USD, VFIAX | `{}` |
| `get_earnings_dates` | SPY, BTC-USD, VFIAX; ayrıca tükenen sayfa | **`None`** |
| `get_sec_filings` | THYAO.IS, SPY, BTC-USD, VFIAX | **`{}` (dict)** |
| `Market(region).status` | US dışındaki 7 bölge | **`None`** |
| Takvim sayfası tükendiğinde | — | Boş **ham şemalı** DataFrame |
| `get_calendar`, `get_sec_filings`, statement uçları (`hide_exceptions=False` ile) | SPY, THYAO.IS ve diğer şirket-olmayanlar | **`HTTPError 404`** / **`IndexError`** — §8.3'teki `call_optional` bunları `empty`ye çevirir |

**İstisna:** `Market("JAPAN")` gibi geçersiz bir bölge `ValueError` fırlatır —
bu `empty` değil **`failed`**'dır. Bölge listesi `MarketRegion` enum'uyla
sınırlıdır ve config doğrulamasında kontrol edilir.

### 8.3 Normalizasyon kuralları (yeni)

| Durum | Kural |
|---|---|
| **Boş sonuç kontrolü** | `raw is None or len(raw) == 0`. `.empty` tek başına yetmez |
| **`sec_filings` tip tutarsızlığı** | `raw if isinstance(raw, list) else []`. Dict üzerinde `for f in raw` anahtarları gezer ve `f["type"]` `TypeError` verir |
| **"Veri yok" istisnası** | `client.call_optional()`: `is_absent_data(exc)` → `None`. Kural yalnızca **404** (`_status_code(exc) == 404`) ve `IndexError("positional indexers are out-of-bounds")` için geçerlidir; başka her hata olduğu gibi yükselir ve hücre `failed` olur. Yalnızca yokluğu meşru olan uçlarda kullanılır: 8 statement, `calendar`, `sec_filings` |
| **Boş takvim sayfası ham şemalıdır** | `_cleanup_df` boş DataFrame'de erken çıkar: `set_index("Symbol")`, `rename`, `to_datetime` **uygulanmaz**. Boşluk kontrolü kolon/index erişiminden **önce** yapılır |
| Kalem anahtarı | `pretty=False` çıktısı, `str(label).strip()`; boş etiket atlanır |
| **`NaN` hücre** | Satır yazılmaz; `item_count` yazılanı sayar |
| float → DECIMAL | `Decimal(repr(float(x))).quantize(Decimal("1E-10"))`. Çıplak `Decimal(repr(x))` numpy 2.x'te `InvalidOperation` verir; quantize edilmezse MySQL sessizce yuvarlar |
| Dönem sonu | Kolon `Timestamp`, tz-naive → `.date()`. tz dönüşümü **yapılmaz** (mali dönem sonu takvimsel etikettir) |
| `earnings_dates` index | tz-aware → `earnings_ts_utc` + `earnings_date_local` + `tz_name`. yfinance yalnızca `EDT`/`EST` kısaltmalarını çözer; başka kısaltmada `tz_localize` patlar → hücre `failed` (try/except ile açıkça) |
| `earnings_dates` tekrarları | Önce birebir `drop_duplicates`, sonra `fact_hash`. Hash girdisi kanonik JSON'dur ve **`NaN` → `null`** dönüşümü zorunludur; aksi hâlde iki satır aynı hash'i alır |
| `calendar['Earnings Date']` | `[0]` → `_start`, `[-1]` → `_end`, `len` → `_count`; `len > 2` → WARNING |
| `sec_filings.epochDate` | **Saniye** cinsinden epoch |
| `filing_id` | `edgarUrl`'den `(\d{10}-\d{2}-\d{6})`; bulunamazsa `sha256(date|type|title)[:32]` |
| `sec_filings` zorunlu alan eksik | Dosyalama atlanır + WARNING (transaction düşürülmez) |
| `exhibits` | `dict[str,str]`; `None`/`{}` olabilir → `exhibit_count=0` |
| **`Market.summary` şekil doğrulaması** | Parse hatasında yfinance ham zarf dict'i (`{'marketSummaryResponse': …}`) döndürebilir. Anahtarların board kodu şekline uyduğu (`len(k) <= 8`, değer `dict`) doğrulanır; uymuyorsa hücre **`failed`**, `empty` değil |
| **`Market.status` JSON'u** | `open`/`close` `datetime` nesneleridir → düz `json.dumps` `TypeError` verir. `HistoryMetadata` ile aynı `default=str` encoder |
| Takvim `NaT`/`NaN` | `NULL`; **PK bileşeni** boşsa satır atlanır + WARNING (§5.4) |
| Takvim sayfa birleştirme | PK üzerinden `dict[pk] = row` ile tekilleştirilir; aksi hâlde `rows_verified < rows_attempted` yanlış `failed` üretir |
| Takvim sembolleri | `strip().upper()`; `RowWriter.known_symbols` ile `is_known` yazılır. FK yoktur |
| `market_cap`, `shares` | Kaynakta `float` → `Decimal(repr(float(x)))` |

### 8.4 Uzun kalem anahtarı

`item_key` 128 karakteri aşarsa satır **yazılmaz**, `item_count`'a
girmez, `WARNING: item_key too long` loglanır ve hücre **`ok`** kalır
(§8.5'in "unmapped keys" deseniyle tutarlı). Hücreyi `failed` yapmak, tek bir
uzun etiket yüzünden dönemin ~200 geçerli kalemini de kaybettirirdi.

Bu yolun bugün tetiklenmesi beklenmez: etiket evreni `const.fundamentals_keys`
ile kapalıdır (375 etiket, max 60 karakter). Kural, kütüphane
yükseltmelerine karşı bir emniyet valfidir ve **iki testle bağlanmıştır**:
biri kapalı evrenin tamamının `ITEM_KEY_LENGTH`'e sığdığını ve alfanumerik
olduğunu doğrular (yükseltme bunu bozarsa test kırılır), diğeri yazılan her
`item_key`'in bu evrende bulunduğunu denetler.

`market_summary`'de tipli kolona alınmamış yeni bir quote alanı görülürse
`WARNING: unmapped market summary keys` loglanır; veri `raw_json`'da durur.

### 8.5 Reconciliation

Değişmez: `rows_verified == rows_attempted` (anahtar varlığı sorgusu),
`rows_skipped` ayrı sayaçta. `financial_facts` sembol başına **8 ayrı
`TableWrite`**'tır (dataset başına bir tane, ~200 satır) → dataset başına 1
doğrulama sorgusu, sembol başına 8. `VERIFY_CHUNK=500` yalnızca daha büyük
yazımlarda böler.

### 8.6 Transaction ve eşzamanlılık

Sembol tarafı: sembol başına tek transaction. Piyasa tarafı: tur başına tek
transaction. İki ayrı advisory lock (`yfin_sync`, `yfin_market_sync`);
`db.advisory_lock` zaten isim parametresi aldığı için `db.py` değişmez.

### 8.7 Sembol silme

`financial_periods` ve `sec_filings` `ON DELETE RESTRICT` taşır; çocuk
tablolar ebeveynden `CASCADE` alır. `yfin symbols purge --force` komutu
tablo listesini `models.symbol_scoped_tables()` ile FK grafından türettiği
için yeni tablolar **kendiliğinden kapsanır**. Takvim ve piyasa tabloları
sembolden bağımsızdır.

---

## 9. Test stratejisi

### 9.1 Normalizasyon testleri (fixture, ağsız)

`scripts/capture_fixtures.py` genişletilir:
`tests/fixtures/{symbol}/{dataset}.json` ve `tests/fixtures/_market/{dataset}.json`.

| Sembol | Kapsadığı kenar durum |
|---|---|
| `AAPL` | Eylül mali yılı; `earnings_dates`'te aynı damgada iki satır (tek fark `Surprise(%)`, EPS'ler `NaN`); 80 SEC dosyalaması; `NaN` yoğun income |
| `MSFT` | Haziran mali yılı; **60 karakterlik `item_key`**; `Ex-Dividend Date` yok |
| `THYAO.IS` | `financialCurrency=USD` ≠ `currency=TRY`; `sec_filings` `{}`; `Dividend Date` yok; tahmin alanları `None` |
| `SPY` | Altı dataset de boş; `earnings_dates` `None` |
| `BTC-USD` | İkinci pazar tipi (kripto): statement `(0,0)`, `calendar` `{}`, `sec_filings` `{}` — "şirket değil" yolunun ETF dışında da geçerli olduğunu kanıtlar |

Kural başına test, özellikle:

- `DECIMAL(38,10)` + `quantize`: `0.156` ve `1.06e14` aynı kolonda; 11.
  basamağın **Python'da** yuvarlandığı (MySQL'e bırakılmadığı)
- `NaN` hücrenin satır üretmediği ve `item_count`'un yazılanı saydığı
- `fact_hash`'in iki `Surprise(%)` satırını ayırdığı; **`NaN → null`
  dönüşümü olmadan hash'lerin çakıştığı** (regresyon testi)
- `sec_filings` girdisi `{}` iken `empty` döndüğü (`TypeError` değil)
- `filing_id`'nin 80/80 `edgarUrl`'den ayrıştığı ve bozuk URL'de hash'e
  düştüğü
- Aynı dosyalamada iki farklı URL'li `EX-99.1` ekinin **ikisinin de**
  yazıldığı (`url_hash`)
- `Market.status` gövdesinin encoder'sız `TypeError` verdiği, `default=str`
  ile geçtiği
- `Market.summary` ham zarf dict'i geldiğinde hücrenin `failed` olduğu
- Boş takvim sayfasının ham kolon adlarıyla geldiği ve kolon erişiminden
  önce yakalandığı
- Takvim satırında PK bileşeni `NaT` ise satırın atlandığı
- `earnings_dates` tz kısaltması tanınmadığında hücrenin `failed` olduğu
- 128 karakteri aşan `item_key`'in atlanıp hücrenin `ok` kaldığı

`market_status` fixture'ı yalnızca `US` için canlıdır; 7 bölgenin `None`
dönüşü sentetik fixture'la test edilir.

### 9.2 Repository testleri (gerçek MySQL, ağsız)

Test şeması **sürece özeldir**: `yfinance_test_<pid>`, oturum sonunda
tamamen düşürülür. Sabit tek şema kullanıldığında iki eşzamanlı pytest
koşusu birbirinin tablolarını `drop_all` ile düşürüyordu
(`ERROR 1684 — table was skipped since its definition is being modified by
concurrent DDL statement`). Kesilen koşulardan kalan şemalar bir sonraki
oturumun başında temizlenir; yalnızca **PID'i artık yaşamayan** şemalar
düşürülür, böylece eşzamanlı bir koşunun şemasına dokunulmaz.

- Aynı dönemi iki kez yazmak: `financial_facts` `skipped`, **`financial_periods`
  yazılır ve `fetched_at` ilerler** (§6.3/b)
- `content_hash` değiştiğinde kalemlerin yeniden yazıldığı
- `replace_scope`'un yalnızca ilgili `(symbol, statement, freq, period_end)`
  kapsamını sildiği, komşu frekansa dokunmadığı
- **`rows` boşken de silmenin yapıldığı** (`scope_values`) ve
  `COUNT(facts) == SUM(item_count)` invaryantının korunduğu
- Bileşik FK'nın `CASCADE` ettiği; `symbols`'tan silmenin `RESTRICT` ile
  engellendiği (`ERROR 1451`)
- **ENUM invaryantı:** `information_schema.COLUMNS`'ta `statement` ve `freq`
  için tam **bir** `COLUMN_TYPE` bulunduğu (§4.4/M2)
- `KeyTextType` sayesinde `Enflasyon` ≠ `Enflâsyon` ≠ `ENFLASYON`
- `ShortHashType`'ın `abcdef…` ≠ `ABCDEF…` ayrımını koruduğu
- `DATETIME(6)` ile aynı saniyede iki `_history` snapshot'ı
- `sync_runs.scope` migration'ının mevcut satırları `'symbols'` yaptığı
- Takvim tablolarına evren dışı sembol yazılıp `is_known=0` işaretlendiği
- `tuple_(...).in_(...)` doğrulamasının `OR` bloğuyla **aynı sayıyı**
  ürettiği
- `EXPLAIN` ile `financial_facts (period_end, item_key)` indeksinin
  kullanıldığı (full scan regresyonu)

### 9.3 Canlı entegrasyon testi (`-m live`)

Dört referans sembol × 11 yeni dataset + `yfin market sync` uçtan uca.
Ardından: satır sayıları, NOT NULL alanların dolu olduğu, FK bütünlüğü,
**`COUNT(financial_facts) == SUM(financial_periods.item_count)`**.

`sec_filings` THYAO.IS'te `empty`, AAPL/MSFT'te dolu; `market_status`
yalnızca `US`'te dolu beklenir. Boş gelmesi beklenen hiçbir hücreye "dolu
olmalı" iddiası kurulmaz.

### 9.4 Statik analiz

`ruff` + `mypy --strict`. `Registry[Dataset[Any]]` ve
`Registry[GlobalDataset[Any]]` tip güvenli çözülmelidir; çıplak `Dataset`
`disallow_any_generics` altında geçmez.

---

## 10. Uygulama sırası ve migration

1. `Registry` sınıfı + çağrı yerleri (`cli.py`, `runner.py`,
   `datasets/__init__.py`) + `test_registry.py`'nin yeniden yazımı
2. `TableWrite.scope_columns`/`scope_values`, `MySQLRowWriter.write` sıra
   düzeltmesi, `_verify`'ın `tuple_` biçimi, `RowWriter.current_hash(key)`
   genelleştirmesi, `SnapshotDataset.key_columns`
3. `models/financials.py`, `models/market.py`, yeni tip fabrikaları
   (`ShortHashType`, `RegionType`, `KeyTextType`) + tek Alembic revizyonu
   (15 tablo + `sync_runs.scope`)
4. `StatementDataset` + `HashGatedDataset` ve sekiz kayıt
5. `calendar`, `earnings_dates`, `sec_filings` dataset'leri
6. `GlobalDataset` / `MarketContext` / `market_runner.py`, `_record_items`'ın
   `DatasetMeta` protokolüne taşınması
7. Altı piyasa dataset'i
8. `cli.py`: `market` alt komutu, `yfin status --scope`, `yfin prune --before`
9. Fixture yakalama + testler

1. ve 2. adımlar davranışı korur ama testleri değiştirir; her adım kendi
başına yeşil bırakır.

### Proje yapısına eklenenler

```
src/yfin/
├── datasets/
│   ├── registry.py                 # Registry sınıfı
│   ├── hash_gated.py               # HashGatedDataset
│   ├── financials/{base,statements,calendar,earnings_dates,sec_filings}.py
│   └── market/{base,status,summary,calendars}.py
├── models/{financials,market}.py
└── market_runner.py
tests/fixtures/_market/
```

---

## 11. Yapılandırma ve kullanım

```
YF_MARKET_REGIONS=US,EUROPE,ASIA,GB,CURRENCIES,CRYPTOCURRENCIES,COMMODITIES,RATES
YF_CALENDAR_LOOKBACK_DAYS=7
YF_CALENDAR_LOOKAHEAD_DAYS=30
YF_CALENDAR_PAGE_LIMIT=100
YF_CALENDAR_MAX_PAGES=5
YF_EARNINGS_DATES_MAX_PAGES=3
YF_PRUNE_ENABLED=false
```

`YF_CALENDAR_PAGE_LIMIT` gerçekten kullanılır (çağrının `limit` argümanı);
sabit yazılmaz. `YF_MARKET_REGIONS` değerleri `MarketRegion` enum'una göre
doğrulanır — geçersiz bölge `ValueError` fırlatır (§8.2).

```bash
yfin db upgrade
yfin sync --symbols AAPL,MSFT --datasets financials
yfin sync --symbols AAPL --datasets calendar,earnings_dates,sec_filings
yfin sync --datasets all                       # piyasa dataset'lerini İÇERMEZ

yfin market sync
yfin market sync --datasets calendars --start 2026-09-01 --end 2026-10-01
yfin status --scope market
yfin prune                                     # yalnızca öksüz news (varsayılan)
yfin prune --calendars-before 2026-01-01 --dry-run --force   # ne silineceğini sayar
YF_PRUNE_ENABLED=true yfin prune --history-before 2026-01-01 # eski snapshot'lar
yfin datasets                                  # her iki registry
```

Yeni çalışma zamanı bağımlılığı yoktur.

---

## Ek A — İnceleme geçmişi

Bu doküman üç bağımsız incelemeden geçti (2026-09-04): mimari tutarlılık
(mevcut koda karşı), MySQL 8.3 şema doğrulaması (gerçek deneylerle) ve
yfinance API doğrulaması (canlı çağrılar + kütüphane kaynağı). Aşağıdaki
bulguların **tümü** ölçümle kanıtlandı ve dokümana işlendi.

**Kapatılan kritik bulgular:**

| Bulgu | Nerede düzeltildi |
|---|---|
| Spec, kodda **var olmayan** API'lere atıf yapıyordu (`apply_table_write(session,…)`, `snapshot_hash_unchanged`, `verify_rows`); gerçek katman `persistence.py` + `RowWriter` | §6 tamamen yeniden yazıldı |
| `runner.py` ve `datasets/__init__.py` de `REGISTRY`'yi import ediyor; "runner.py değişmez" iddiası yanlıştı | §6.1, §7.1 |
| `SnapshotDataset` `financial_periods`'ta çalışmaz: karşılaştırılan tablo = yazılan tablo; atlanacak olan `_history` değil çocuk tablo | §6.3 — ayrı `HashGatedDataset` |
| `replace_scope` boş satır listesinde **hiç silmiyor** (`persistence.py:78` erken çıkış) → `SUM(item_count)` invaryantı kırılıyordu | §6.2 — sıra düzeltmesi + `scope_values` |
| `get_earnings_dates` sayfalaması aynı `Ticker`'da **no-op**; `_earnings_dates` yalnızca `limit`'e anahtarlı | §6.5, §7.4 — sayfa başına taze Ticker |
| `earnings_calendar` `filter_most_active=True` varsayılanı yalnızca `offset==0`'da uygulanıyor → sayfa 0 ile 1+ farklı evrenlerden geliyor, 1–100. satırlar hiç çekilmiyor | §6.5 — `filter_most_active=False` |
| "Sayfalama `len < limit` ile bitmez" iddiası **yanlış nedene** dayanıyordu | §4.2 — iddia geri çekildi |
| Kalem etiketi max uzunluğu 51 değil **60** (MSFT bilanço) | §4.1, §5.2 — `VARCHAR(128)` |
| `last_value` MySQL 8 **rezerve kelimesi** (`ERROR 1064`) | §5.4 — `last_reported` |
| ENUM listeleri ayrışırsa FK **ordinal** üzerinden yanlış satıra bağlanıyor, uyarı yok | §5.1 — tek `StrEnum` + şema invaryant testi |
| `sec_filing_exhibits` PK'sı aynı dosyalamadaki ikinci `EX-99.1`'i düşürüyor (`ERROR 1062`) | §5.3 — `url_hash` PK'ya |
| PK'ya giren serbest metinler `ai_ci`: `Enflasyon` = `Enflâsyon` = `ENFLASYON` | §5 — `KeyTextType` |
| `financial_facts (period_end)` sorgusu full scan (600k satırda 148 ms) | §5.7 — `(period_end, item_key)` |
| §5.6'daki 4 indeks PK'nın ön eki, gereksiz | §5.7 — kaldırıldı |
| `DECIMAL(38,10)` 11. basamağı sessizce yuvarlıyor | §5.6, §8.3 — Python'da `quantize` |
| `fact_hash` girdisinde `NaN → null` yapılmazsa iki satır aynı hash'i alıyor | §5.3, §8.3 |
| `mypy --strict` çıplak `Dataset`'i reddediyor (`Dataset[RawT]` generic) | §6.1 |
| `bootstrap=None` dalı mevcut kodda çöküyor, davranışı tanımsızdı | §6.1 |
| Piyasa tarafında `sync_run_items` granülerliği üç farklı biçimde ima ediliyordu | §6.4, §7.2, §8.1 — (dataset × tablo × bölge) |
| `symbol_count` yazılmazsa `yfin status` yanıltıcı olur | §7.2 — `symbol_count=0` |
| `item_key` uzunluk kuralı mevcut runner'da `failed` üretemiyordu | §8.4 — satır atlanır, hücre `ok` |
| `Market.summary` parse hatasında **ham zarf dict** dönebiliyor | §8.3 — şekil doğrulaması |
| Boş takvim sayfası `set_index`/`rename` uygulanmamış ham şemayla dönüyor | §8.3 |
| `Market(region)` geçersiz bölgede `ValueError` (empty değil) | §8.2 |
| `earnings_dates` tz parse'ı yalnızca EDT/EST tanıyor | §8.3 |
| `info` **3 wire isteği**; sembol başına 11 değil ~14 istek | §4.1, §7.5 |
| Piyasa sync "~15 istek" değil, 16 + takvim sayfaları | §7.5 |
| `Market.status` serileştirme hatası `timezone`'dan değil `datetime`'dan | §5.6 |
| `sec_filings` zorunlu alanı eksikse tüm transaction düşerdi | §5.3, §8.3 |
| Takvim PK bileşeni `NaT` gelirse `ERROR 1048` ile transaction düşerdi | §5.4, §8.3 |
| Sayfalar arası PK tekrarı yanlış `failed` üretirdi | §8.3 |
| `_verify`'ın `OR` blokları `tuple_` biçiminden 4,4× yavaş | §6.2 |
| Tablo sayıları yanlıştı (13 → 15; mevcut 15 → 16) | §1 |
| `test_registry.py` sabit sayı iddiaları kırılacaktı | §6.1, §10 |
| `yfin market prune` / `market status` gereksiz ikinci komut ailesiydi | §11 — `yfin prune --before`, `yfin status --scope` |

**Uygulama sırasında ortaya çıkan ve spec'e işlenen bulgu:**

| Bulgu | Nerede karşılandı |
|---|---|
| `hide_exceptions=False` ayarı, "şirket değil" durumunu `HTTPError 404` / `IndexError`'a çeviriyor; ilk canlı koşuda 4 sembolde **10 hücre `failed`** oldu | §4.2, §8.2, §8.3 — `client.call_optional` + `is_absent_data`; düzeltme sonrası koşu: **0 failed**, 38 ok / 23 empty / 27 skipped |

---

## Ek B — Değerleme ölçütleri (`get_valuation_measures`)

Bu uç ne bu spec'in ne de AH spec'inin kapsam listesinde yer alıyordu: AH
§1 onu "Financials bölümünün parçası" diyerek erteledi, bu doküman ise hiç
ele almadı. Sonradan kapatıldı; kararlar aşağıdadır.

### B.1 Yeni tablo YOKTUR

Çerçeve finansal tablolarla **aynı şekildedir** — index kalem etiketi,
kolonlar dönem. Kaynak da aynıdır: yfinance 1.7.0 veriyi statement'larla
aynı `fundamentals-timeseries` ucundan çeker
(`scrapers/quote.py:739-830`), değerler ham `float`'tır (eski
key-statistics kazımasındaki `'3.76T'` biçimli dizeler değil). Bu yüzden
`financial_periods` / `financial_facts` EAV'si **olduğu gibi** yeniden
kullanılır ve `StatementDataset.normalize` tek satır bile değişmeden
çalışır.

### B.2 Şema değişikliği tek bir ENUM genişlemesidir

`StatementKind`'e `VALUATION = "valuation"` **sona** eklenir. MySQL native
ENUM'da değer sırası ORDINAL'dir ve `fk_financial_facts_period` bileşik
FK'si bu kolonu taşır; araya girmek mevcut satırların anlamını kaydırırdı
(§5.1'in aynı gerekçesi). MySQL FK'li bir kolonun tipini değiştirmeyi
reddettiği için migration iki `ALTER`'ı `FOREIGN_KEY_CHECKS=0` arasında
çalıştırır. `downgrade` önce satırları siler (FK açıkken, `ON DELETE
CASCADE` çocukları götürsün diye), **sonra** ENUM'u daraltır — ters sırada
MySQL `'valuation'` satırlarını boş dizeye çevirip veriyi sessizce bozardı.

### B.3 `Current` kolonu YAZILMAZ

Kaynak `Current` + `M/D/YYYY` kolonları döndürür. `Current`'ın dönem sonu
tarihi yoktur (PK bileşeni boş kalırdı) ve değeri çekim anındaki fiyata
bağlıdır — kalıcı arşivde bayatlayan bir sütun, `price-history-service-design.md`
§1/K3'te `adj_close`'un dışlanma gerekçesiyle aynı sınıfta. Güncel piyasa değeri zaten
`info.marketCap` ile gelir.

Kolon etiketi `pd.Timestamp`'e **bırakılmaz**: `'1/2/2026'` gibi bir
etikette ay/gün sırası pandas'ın varsayımına kalırdı. Kaynak etiketi
`f"{d.month}/{d.day}/{d.year}"` ile ürettiği için biçim `%m/%d/%Y` olarak
açıkça verilir; çözülemeyen etiket WARNING ile düşürülür ve hücre `ok`
kalır.

### B.4 `periods=None` — kırpma yoktur

yfinance varsayılanı `periods=5`, çerçeveyi **istemcide** kırpar
(`quote.py:640-644`). Tüm geçmiş zaten aynı istekle geldiği için kırpmak
bedava veriyi atmak olurdu; ek istek maliyeti **yoktur**.

### B.5 Kayıtlı frekanslar: yalnızca `annual` + `quarterly`

| Çıkarılan | Gerekçe (ölçüm) |
|---|---|
| `trailing` | AAPL'de 13 kolon ve tarihleri **düzensiz** (9/2, 9/1, 8/27, 8/11, 8/10, 7/31/2026, 10/3/2025); üstelik aynı kolonda ölçütlerin bir kısmı NaN — Market Cap 9/2'de, Trailing P/E 9/1'de dolu. Bunlar dönem sonu **değil anlık gözlem** damgalarıdır: her koşu yeni `period_end` satırları üretir ve EAV'nin (sembol, tablo, frekans, dönem) tanesini anlamsızlaştırırdı. |
| `monthly` | `StatementFreq`'e yeni bir üye eklemek iki tabloda **paylaşılan** native ENUM'u genişletir. İhtiyaç doğarsa `MONTHLY` eklenip `_SPECS` listesine bir satır yeter. |

### B.6 `valuation` ayrı bir alias'tır

`financials` alias'ına **katılmaz**: kaynak dokümantasyonun Financials
bölümünde yer almaz ve ayrı bir HTTP isteğidir; katmak o adın mevcut
maliyetini sessizce iki istek büyütürdü. `--datasets all` ikisini de alır.

### B.8 `currency` KOTASYON para birimidir, raporlama değil

`financial_periods.currency` statement satırlarında
`info.financialCurrency`'dir (§5.6). Valuation satırlarında bu **yanlış
olurdu** ve ilk uygulamada yanlıştı. Ölçüm (`THYAO.IS`):

| alan | değer |
|---|---|
| `info.financialCurrency` | `USD` |
| `info.currency` | `TRY` |
| `info.marketCap` | 4,08e11 |
| valuation `Market Cap` (Current) | 4,14e11 |

İki piyasa değeri aynı mertebede; USD karşılığı bunun ~1/30'u olurdu.
Yani değerleme ölçütleri borsanın **kotasyon** para birimindedir. Oranlar
(P/E, P/S, PEG) birimsizdir; kolonun anlamı iki parasal ölçüt (`Market
Cap`, `Enterprise Value`) içindir. `valuation.py` bu yüzden kendi
`quote_currency()` politikasını taşır ve `statements.financial_currency`'yi
KULLANMAZ — aynı `info` önbelleğini paylaştığı için ek istek doğurmaz.

### B.7 Ölçülen boş durumlar

`SPY`, `BND`, `BTC-USD`, `^GSPC` — dördünde de kaynak `(0,0)` DataFrame
döndürür, exception değil. Yani `empty`, `failed` değil (§8.2).

