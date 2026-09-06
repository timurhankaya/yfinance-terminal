# yfinance Sector & Industry → MySQL — Tasarım Dokümanı

- **Tarih:** 2026-09-04
- **Durum:** Ölçüm tabanlı taslak; kullanıcı onaylarından geçti
- **Proje kökü:** `~/Projects/learn/yfinance/`
- **Temel doküman:** `2026-09-04-yfinance-mysql-etl-design.md` (bundan sonra **T**, ör. T§8.3)
- **Önceki genişletmeler:** `2026-09-04-yfinance-financials-market-design.md` (**F**),
  `2026-09-04-proxy-pool-and-yfinance-advanced-design.md` (**P**),
  `2026-09-04-yfinance-analysis-holdings-design.md` (**AH**)
- **Kapsam:** `https://ranaroussi.github.io/yfinance/reference/yfinance.sector_industry.html`
  sayfasındaki `yfinance.Sector` ve `yfinance.Industry` bölümlerinin tamamı

---

## 0. Önkoşullar

Bu doküman **T**, **F**, **P** ve **AH**'yi genişletir, hiçbirinin yerine
geçmez. Uygulama aşağıdakilerin kodda bulunmasını varsayar; hepsi
2026-09-04'te doğrulandı:

| Önkoşul | Nerede |
|---|---|
| `Registry` sınıfı, `SYMBOL_DATASETS` / `MARKET_DATASETS` | `datasets/registry.py` |
| `TableWrite.scope_columns` / `scope_values` / `monotonic_columns` | `datasets/base.py:65-83` |
| `HashReader.current_hash(table, key: Mapping)` → `RowWriter` protokolü | `persistence.py:51-58`, `:76`, uygulama `:234` |
| `SymbolLookup.known_symbols()` → `RowWriter` protokolü | `persistence.py:67` |
| `AsOfDataset`, `GATE_TABLE`, `VOLATILE_COLUMNS`, `asof_produces()` | `datasets/asof_base.py` |
| `GlobalDataset`, `MarketContext`, `MarketContext.for_region()` | `datasets/market/base.py` |
| `market_runner.py`, `GLOBAL_SCOPE_MARKER`, `RunScope` | `market_runner.py`, `models/sync.py` |
| `_record_items` (`DatasetMeta` + `symbol` alır) | `runner.py:331` |
| `_failed_records` / `_skipped_records` (**registry parametreli**) | `runner.py:379`, `:407` |
| `ItemRecord` / `write_items` | `runner.py:56-66`, `:612-643` |
| `errors.classify_error()` / `is_absent_data()` / `client.call_optional()` | `errors.py`, `client.py:74-86` |
| `nz.canonical_json()` (`sort_keys=True`), `nz.content_hash()` | `normalize.py:317-341` |
| `KINDS` tablosu (`dec`, `big`, `int`, `ubig`, `dt`, `str*`, `text`) | `models/kinds.py` |
| `SymbolType()`, `AsciiKeyType()`, `RegionType()`, `HashType()`, `RawJsonType()` | `models/base.py` |
| `is_known` deseni (FK'siz sembol + DB'den doldurma, **hash gövdesine dâhil**) | `datasets/news.py:202-210`, `datasets/funds.py:414-446` |
| `prune_asof` / `asof_table_datasets` / `_asof_protected` | `prune.py:86-105`, `:129-137`, `:150-158` |
| Alembic head revizyonu | `migrations/versions/20260904_1700_history_metadata_tz_name.py` (`e7f2a94c1b83`) |

Bu tipler ve sözleşmeler **yeniden tanımlanmaz**. Bu spec **yedi** ekleme /
değişiklik yapar; hiçbiri mevcut dataset'lerin davranışını değiştirmez:

| # | Ekleme | Nerede | Mevcut davranışa etkisi |
|---|---|---|---|
| 1 | `DomainDataset` + `DomainContext` | §6.1 | Yok (yeni ABC) |
| 2 | `AsOfGate` mixin'inin `AsOfDataset`'ten ayrılması + `asof_gate_table` / `asof_gate_key_columns` / `gate_identity()` | §6.2 | Yok (varsayılanlar bugünkü davranış) |
| 3 | `VOLATILE_COLUMNS`'a `first_seen_at` | §6.2 | Yok — bugün hiçbir **veri** tablosunda `first_seen_at` kolonu yok, yalnız `asof_state`'te var (`models/asof.py:42`) |
| 4 | `asof_produces(*tables, gate=…)` imzası | §6.2 | Yok (varsayılan `GATE_TABLE`) |
| 5 | Üçüncü registry + `domain_runner` | §6.3, §6.7 | Yok |
| 6 | `ItemRecord.region` + `_record_items`/`_failed_records`/`_skipped_records`/`write_items`'a opsiyonel `region` | §8.1 | Yok (varsayılan `None`) |
| 7 | `prune_asof` ailesinin (registry, kapı tablosu, kapsam kolonu) üçlüsüyle parametrelenmesi | §11 | Yok (mevcut çağrı varsayılanları kullanır) |

Ayrıca **şema tarafında**: `RunScope.DOMAIN` enum değeri (`models/sync.py`),
`sync_run_items.region` kolonu (§5.9).

---

## 1. Amaç

Kurulu veri hattına yfinance'ın **sektör** ve **endüstri** verilerini
eklemek. Mevcut ilkeler değişmez: eksiksizlik, tip güvenliği, idempotency,
**sembol kodu üzerinden ilişki**, makine tarafından doğrulanabilir "eksiksiz
yazıldı" iddiası.

Bu genişletme mevcut mimarinin dört varsayımını zorluyor; dördü de açıkça
çözülüyor:

1. **Her dataset ya sembol-kapsamlı ya piyasa-kapsamlıdır.** Sektör ve
   endüstri üçüncü bir eksendir: 156 anahtar, her biri kendi HTTP isteği
   (§6.1).
2. **Anahtar evreni ya kullanıcıdan ya kütüphaneden gelir.** Kütüphanenin
   endüstri anahtar listesi **hatalıdır**: 145 anahtarın 32'si canlı API'de
   404 veriyor (§4.1). Evren, sektör yanıtından **keşfedilerek** kurulur
   (§6.5).
3. **yfinance'in property'leri kaynağın tamamını verir.** Vermiyor:
   `Sector`/`Industry` sınıfları ham yanıtın **23 alanını** atıyor; bunların
   11'i iki **tam bloğu** (`performance`, `performanceOverviewBenchmark`)
   oluşturuyor. Üstüne top-level `key` alanı da atılıyor → **24** (§4.3).
   Ham JSON `YfData` üzerinden ayrıştırılır (§6.4).
4. **Geçersiz parametre hata üretir.** Üretmiyor: geçersiz `region` sessizce
   **US verisini** döndürüyor (§4.1). Run başında ampirik doğrulama gerekir
   (§6.6).

### Kapsam içi

**5 dataset** (1'i bootstrap):
`domain_taxonomy`, `sector_profile`, `sector_rankings`, `industry_profile`,
`industry_rankings`.

**2 alias:** `sector`, `industry`.

**8 yeni tablo** (7 veri + 1 kapı), `sync_runs.scope` ENUM'una 1 değer,
`sync_run_items`'a 1 kolon, `symbols`'a 156 satır (şema değişikliği yok).
Toplam tablo 54 → **62** (`Base.metadata` ile 2026-09-04'te sayıldı).

### Kapsam dışı — gerekçeli, tahminle değil ölçümle

| Çıkarılan | Gerekçe (ölçüm) |
|---|---|
| `industries` bloğunun *"All Industries"* satırı için ayrı tablo | O satırın `ytdReturn` (`0.23698471`) ve `regMarketChangePercent` (`-0.0071075135`) değerleri, **aynı çekimde** `performance.ytdChangePercent` / `performance.regMarketChangePercent` ile birebir özdeş ölçüldü. Redundant |
| `sector.industries[]` satırları için ayrı tablo (`domain_children`) | Her satırın `marketWeight`/`ytdReturn`/`regMarketChangePercent` değeri, o endüstrinin kendi `domain_metrics` satırıdır. Semiconductors: sektör çerçevesinde `0.37623784`, endüstri `overview`'unda `0.37629837` (iki farklı çekim anı, aynı büyüklük) |
| `Sector.ticker` / `Industry.ticker` property'si | `Ticker(self._symbol)` döndüren bir kolaylık; ürettiği veri `symbols` tablosuna 156 satır yazıldıktan sonra mevcut `history`/`info`/`fast_info` dataset'lerinden **zaten** gelir (§5.8) |
| Bölge ekseninin profil tablolarına girmesi | `overview`, `performance`, `performanceOverviewBenchmark`, `industries`, `researchReports` US/GB/DE/JP/TR'de **birebir aynı** ölçüldü |
| Bölge ekseninin `domain_report_links`'e girmesi | Rapor kimlikleri 5 bölgede birebir aynı sırayla döndü |
| `market_runner`'ın bölgeyi `symbol` alanına yazan davranışının düzeltilmesi | Bu spec'in amacına hizmet etmez ve mevcut denetim sorgularını kırardı. Bilinçli kapsam dışı (§5.9) |
| Sektör/endüstri anahtarlarının kütüphane sabitinden seed edilmesi | 145 anahtarın 32'si 404 (§4.1). §9.1'de bir regresyon testi bunu kalıcı olarak sabitler |

---

## 2. Kararlar ve gerekçeleri

| Karar | Seçim | Gerekçe |
|---|---|---|
| Varlık modeli | **Birleşik taksonomi**: `domains` tablosu, `domain_type` ENUM'u, `parent_key` self-FK | Sektör ve endüstri `overview` kolon setleri özdeş; `topCompanies` kolon setleri **birebir aynı** ölçüldü. Kod tabanının kendi kuralı: özdeş kolon seti → tek tablo + ayırıcı ENUM (`institutional_holders`+`mutualfund_holders`, `earnings_estimate`+`revenue_estimate`) |
| `domain_type` PK'da mı | **Hayır** | 11 sektör ve 145 endüstri anahtarı ölçümle ayrık (kesişim = ∅); sembolleri de ayrık. PK'ya konsaydı `parent_key` self-FK'si iki kolonlu olur ve her JOIN'e taşınırdı |
| Veri kaynağı | **Ham JSON**, `YfData.get_raw_json` üzerinden | yfinance property'leri **24 alanı** atıyor; 11'i iki tam blok (§4.3). `Domain._fetch` zaten `YfData`'nın ince sarmalayıcısı; ham JSON'a inmek yfinance'ten çıkmak değil, aynı HTTP/proxy/cookie/curl_cffi katmanını kullanmaktır |
| Endüstri anahtar kaynağı | **Yalnız** sektör yanıtının `industries[].key` alanı | `SECTOR_INDUSTY_MAPPING_LC` 32 anahtarda 404 veriyor (§4.1) |
| Sektör anahtar kaynağı | Kendi modülümüzdeki `SECTOR_KEYS` sabiti + eşitlik testi | "Sektörleri listele" ucu yok. 11/11 canlıda doğrulandı; test kütüphaneyle sapmayı yakalar |
| Zaman ekseni | **as-of günlük**, PK'da `as_of_date` | `market_cap`/`market_weight` her çekimde değişiyor (11/11 sektörde 15 dk arayla); `snapshot + _history` çifti saatlik cron'da günde 24 satır/varlık üretirdi |
| `as_of_date` kaynağı | `America/New_York` günü | 6/6 domain sembolünün timezone'u `America/New_York`, benchmark `S&P 500`. UTC günü 23:30 ve 00:30 koşularını aynı işlem günü için ikiye bölerdi |
| Kapı tablosu | **Yeni** `domain_asof_state`, PK `(domain_key, dataset, region)` | `asof_state`'in anahtarı `(symbol, dataset)` ve `AsOfDataset._gate_write` kapı sembolünü `row["symbol"]`'den okur — burada `symbol` **şirketin** sembolü. Ayrıca bölge ekseni oraya sığmaz. `asof_base.py`'nin kendi gerekçesinin aynısı |
| Bölge ekseni | Yalnız liste tablolarının PK'sında | `overview`/`performance`/`industries`/`researchReports` 5 bölgede birebir aynı ölçüldü |
| Bölge doğrulaması | Run başında **ampirik prob** | `region='XX'`, `'EUROPE'`, `''` hiç hata vermeden US listesini döndürüyor; biçim kontrolü `XX`'i geçirirdi |
| 404 davranışı | **`failed`**, `empty` değil — `call_optional` kullanılmaz | Anahtar aynı koşuda kendi keşfimizden geliyor; 404 "taksonomi bayat" demektir. AH§8.4'ün kuralı sembol tarafı içindir (anahtar kullanıcıdan gelir, modül yokluğu meşrudur) |
| `topPerforming` + `topGrowth` | Tek `domain_top_movers`, `rank_type` **PK'da** | 24 endüstrilik tam-liste ölçümünde **50 ortak sembolün 8'inde** iki uç farklı `ytdReturn` bildiriyor (145'e ölçeklenince ~300 ortak / ~48 farklı); `rank_type` PK'da olmasaydı biri sessizce kaybolurdu. `fund_metrics`'in `section`'ı PK'ya koymasıyla aynı gerekçe |
| `topETFs` + `topMutualFunds` | Tek `domain_top_funds`, `fund_type` PK'da | Kolon setleri birebir aynı; sembol kümeleri bugün ayrık ama bu yarın da ayrık kalacağının kanıtı değil |
| Analist raporları | Rapor tablosu + bağ tablosu | Günde 624 rapor satırı, yalnız **516'sı tekil**; 37 tekil sektör raporunun **hepsi** bir endüstride de görünüyor (%100 örtüşme) → tek tabloda `ERROR 1062`. `reportTitle` **23 570** karaktere kadar ölçüldü; tek tablo onu her sahip için tekrar yazardı |
| `rating`, `investment_rating`, `target_price_status` | `VARCHAR`, ENUM değil | 5 / 4 / 4 değer ölçüldü; kapalı liste olduğunun kanıtı yok (AH'nin `action` kararının aynısı) |
| `is_known` | FK yok + DB'den doldurulan bayrak | `SGE.L`, `285A.T`, `ODINE.IS`, `0P0001WO1I` evren dışı; FK olsaydı tek yabancı sembol turun transaction'ını düşürürdü (`news_symbols` gerekçesi) |
| `is_known` hash'te mi | **Evet** | `funds.py:432-437`'nin açıkça kurduğu ilke: "bayrak hash gövdesine girer, böylece evren değiştiğinde kapı açılır ve satırlar güncellenir". Dışlansaydı kullanıcı `SGE.L`'i `symbols`'a ekledikten sonra `is_known` **hiç güncellenmezdi** — kapı `skipped` derdi |
| `raw_json` kapsamı | Yanıtın **liste-dışı** kısmı (`key`, `name`, `symbol`, `sectorKey`, `sectorName`, `overview`, `performance`, `performanceOverviewBenchmark`); hash'e **dâhil** | `nz.canonical_json` yalnız **sözlük** anahtarlarını sıralar, liste sırasını korur (`normalize.py:317-329`). Tam zarf saklansaydı `topCompanies` sırası (11 sektörün 8'inde 15 dk'da değişti) kapıyı **her koşuda** açardı ve as-of mekanizması sessizce hiç çalışmazdı. Liste blokları zaten %100 tipli kolonlara gidiyor (§4.3) |
| "All Industries" eleme kuralı | **`key` alanının yokluğu** | yfinance ada bakıyor (`!= 'All Industries'`), bu dile bağlı. Ölçüm: 13 satırın 12'sinde `key` ve `symbol` var, o satırda ikisi de yok |
| `sync_run_items.symbol` içeriği | **Domain sembolü** (`^YH31130020`), anahtar değil | Kolon `VARCHAR(32)`; 5 endüstri anahtarı bunu aşıyor (en uzun 37). Ayrıca "ilişkiler sembol kodu üzerinden" ilkesi denetim katmanında da geçerli olur |
| Çalıştırma mimarisi | Üçüncü registry + `domain_runner` | 156 anahtar `market_runner`'ın "6 dataset, tek tur" varsayımını bozar; `SyncContext.ticker` domain için anlamsızdır |
| Paralellik / shard | **Yok** | 156 istek, tek process, tek proxy. Sembol tarafındaki kuyruk/backpressure makinesi burada karşılığı olmayan bir karmaşıklık olurdu (`market_runner`'ın gerekçesi) |

---

## 3. Ortam

**T**§3 ile aynı: MySQL 8.3.0 (Docker, host `localhost`, şema `yfinance`,
test şeması `yfinance_test`), Python 3.13.5, yfinance 1.7.0, pandas 3.0.5,
numpy 2.5.2.

Tüm §4 bulguları 2026-09-04'te canlı Yahoo çağrılarıyla üretildi:
**11 sektör × 2 tekrar**, **145 endüstri**, **5 bölge** (US, GB, DE, JP, TR),
**4 geçersiz anahtar**, **4 geçersiz bölge**, **6 domain sembolü** —
toplam **223 istek** (keşif 11×2 + 145 + 32 düzeltme; bölge 5×2; geçersiz anahtar 4×2; ham zarf 2; domain sembolü 6; mover tam-listesi 24; çeşitli 5).

Uçlar:
`GET https://query1.finance.yahoo.com/v1/finance/sectors/{key}`
`GET https://query1.finance.yahoo.com/v1/finance/industries/{key}`
Parametreler: `formatted=true`, `withReturns=true`, `lang=en-US`, `region={REGION}`.

---

## 4. API keşif bulguları

### 4.1 Doğrulanmış davranışlar

| Bulgu | Gözlem | Tasarıma etkisi |
|---|---|---|
| **Kütüphane anahtar listesi hatalı** | `SECTOR_INDUSTY_MAPPING_LC`'nin 145 endüstri anahtarının **32'si 404**. Neden: `const.py:313-318` `k.lower().replace('& ','').replace('- ','').replace(', ',' ').replace(' ','-')` uyguluyor ama **em-dash (`—`) ve `&` karakterine dokunmuyor**: `software—application`, `banks—regional`, `oil-gas-e&p`. Canlı karşılıkları `software-application`, `banks-regional`, `oil-gas-e-p` | §6.5: anahtarlar **yalnız** sektör yanıtından keşfedilir |
| Etkilenen sektörler | `utilities` **6/6**, `real-estate` 11/12, `financial-services` 7/15, `consumer-defensive` 3/12, `healthcare` 2/11, `technology` 2/12, `energy` 1/8 | `utilities` fixture olarak seçildi (§9.1) |
| Canlı taksonomi | 11 sektör, **145 endüstri**; sektör başına sayılar kütüphane sabitiyle **birebir aynı** (14/7/23/12/8/15/11/25/12/12/6) | Sabit sayıca doğru, anahtarca yanlış |
| **`industriesCount` çapraz doğrulaması** | 11/11 sektörde `overview.industriesCount` == `len(industries[])`; toplam **145** | §8.3: eksiksizliğin beklenen değerini API'nin kendisi veriyor |
| Sektör/endüstri anahtar çakışması | **Yok** (kesişim = ∅); sembol kesişimi de yok | §5.1: `domain_key` tek başına PK |
| Anahtar büyük/küçük harf duyarlı | `TECHNOLOGY` → **404**; `technology` → 200 | §5.1: `domain_key` `ascii_bin` |
| Domain sembolleri gerçek ticker | `^YH311` (sektör, 6 hane), `^YH31130020` (endüstri, 11 hane). `quoteType=INDEX`, `exchange=YHD`, `currency=USD`, `tz=America/New_York` — 6/6 sembolde (**ayrı bir canlı `fast_info` ölçümü**; bu alanlar sector/industry yanıtında yoktur); `history(period='5d')` **(5,7)** döndürüyor | §5.8: 156 satır `symbols`'a yazılır, ilişkiler FK'li olur |
| **Geçersiz bölge sessizce US döndürüyor** | `region='XX'`, `'EUROPE'`, `''`, `'us'` → `topCompanies` US ile **birebir aynı** (NVDA, AAPL, …), hata yok | §6.6: run başında ampirik prob |
| Bölge kapsamı | `overview`, `performance`, `performanceOverviewBenchmark`, `industries`, `researchReports` 5 bölgede **birebir aynı**; yalnız `topCompanies`, `topETFs`, `topMutualFunds`, `topPerformingCompanies`, `topGrowthCompanies` değişiyor | §5: bölge yalnız liste tablolarının PK'sında |
| Bölgeye göre liste boyutu | `technology` `topCompanies`: US 50, GB 50, DE 50, JP 50, **TR 38**. `topETFs`: US 10, **GB 0**, **DE 0**, **JP 0**, **TR 0**. `topMutualFunds`: US 10, GB 0, **DE 1**, JP 0, TR 0. `semiconductors` `topCompanies`: US 49, **GB 2**, DE 4, JP 22, **TR yok** | Boş blok `empty`, hata değil |
| Geçersiz anahtar | 404 (`nonexistent-key`, `technolgy`, `TECHNOLOGY` — sektör ve endüstri uçlarında) | §8.2: `failed` |
| Boş anahtar | `KeyError('data')`, HTTP durumu **yok** | `classify_error` → `DATA` → `failed` |
| `overview` alanları | 156/156 varlıkta `companiesCount`, `marketCap`, `messageBoardId`, `description`, `marketWeight`, `employeeCount` **dolu**. `industriesCount` **yalnız sektörde var**: endüstri `overview`'unda anahtar **hiç yok** (6 anahtar vs sektörde 7) — yfinance'in `.get()` çağrısı onu `None`'a çeviriyor, ham JSON'da yokluk | §5.2: `industries_count` nullable; §7.6: `_MAPPED_OVERVIEW_KEYS` iki varyant tanır |
| `overview` oynaklığı | `marketCap` ve `marketWeight` 11/11 sektörde 15 dk arayla **değişti** (`28795478212608` → `28829932322816`) | §2: as-of günlük |
| `topCompanies` oynaklığı | 11 sektörün **8'inde** 15 dk arayla **sıra** değişti; `technology`'de **küme de** değişti | §7.4: hash girdisi `key_columns`'a göre sıralanır. §6.6: bölge probu bu yüzden liste eşitliği **kullanamaz** |
| `researchReports` | 156/156 varlıkta **tam 4** rapor | — |
| Rapor paylaşımı | Günde **624** rapor satırı (11×4 + 145×4), yalnız **516'sı tekil**. 44 sektör raporunun 37'si tekil; **37 tekil sektör raporunun hepsi** bir endüstride de görünüyor (sektör∩endüstri = 37, %100) | §5.6: rapor + bağ tablosu |
| Rapor bölge-değişmezliği | Aynı 4 kimlik, aynı sırayla, 5 bölgede | §5.6: bağ tablosunda bölge yok |
| Rapor oynaklığı | 11 sektörün 2'sinde 15 dk arayla kimlikler değişti | as-of günlük yeterli |
| Endüstri yanıtının liste blokları | **Üç**: `topCompanies`, `topPerformingCompanies`, `topGrowthCompanies`. `topETFs`/`topMutualFunds` endüstri yanıtında **yoktur** (top-level anahtar listesiyle doğrulandı) | §5.4: `domain_top_funds` yalnız sektör — veri kaybı değil |
| `infrastructure-operations` | `companiesCount=1`, `employeeCount=18`; **üç liste bloğunun hiçbiri yok** | §7.5: `empty`, `failed` değil |
| `ytdReturn` uç değer | ELOX (biotechnology) **9999.0** | Sentinel sayılmaz, olduğu gibi yazılır |
| `growthEstimate` uç değer | RELL **81.5**, XPER **80.0**, alt uç **−9.999999999999998** | `DECIMAL(28,12)` |
| Mover listelerinde çakışma | 24 endüstrilik **tam-liste** ölçümü: **50 ortak sembol, 8'inde `ytdReturn` farklı**. (İlk taslaktaki 118/24 sayısı yalnızca her listenin **ilk 3 satırından** hesaplanmıştı — ölçüm scripti `head(3)` kaydediyordu; 145'e ölçeklenmiş tahmin ~300 ortak / ~48 farklı) | §5.5: `rank_type` PK'da |
| Fon sembolü ticker olmayabilir | `healthcare` `topMutualFunds`: `0P0001WO1I`, `0P0001WO1G` (Morningstar kimliği, 10 hane) | `is_known=0`, `SymbolType(32)` |
| Fon adı eksik | Günlük 220 fon satırının (110 ETF + 110 yatırım fonu) **7'sinde** `name` yok; yedisi de **yatırım fonu** tarafında (`FFNQX`, `FFNUX`, `FFOBX`, `VARCX`, `0P0001WO1I`, `0P0001WO1G`, `FFOMX`) | NULL |
| `rating` eksik | Sektörde 550 satırın **12'sinde**; `semiconductors`'ta **2/49** | NULL |
| Ölçülen `rating` değerleri | `Strong Buy`, `Buy`, `Hold`, `Underperform`, `Sell` | `VARCHAR(32)` |
| Ölçülen `investmentRating` | `Bullish`, `Neutral`, `Bearish`, yok | `VARCHAR(32)` |
| Ölçülen `targetPriceStatus` | `Maintained`, `Increased`, `Decreased`, yok | `VARCHAR(32)` |
| String uzunlukları | `domain_key` max **37** (`utilities-independent-power-producers`); `name` max **40**; `description` max **446**; `messageBoardId` max **15**; `report_id` max **50**; `headHtml` max **59**; **`reportTitle` max 23 570** (104 rapor üzerinde; medyan 281, 2'si 1006'yı aşıyor) | §5 tipleri; `reportTitle` **`MEDIUMTEXT`** olmak zorunda (§5.6) |
| Sayısal aralıklar | `marketCap` 1,23×10⁸ … 2,88×10¹³; `companiesCount` 1 … **1517** (`financial-services`; `technology` 853 yalnızca o sektörün değeri); `employeeCount` 18 … 11 895 040; `marketWeight` 1,32×10⁻⁵ … 0,746 | §5.2 tipleri |
| `industries[].marketWeight` toplamı | 11/11 sektörde **tam 1,0000** | — |
| Endüstri `topCompanies` boyutu | 144 dolu endüstride toplam **2743** satır, ortalama **19,05**; 1 … 49 arasında | §5.10 satır tahmini |
| Mover listesi boyutu | 145 endüstride **685 + 685 = 1370** satır/gün; 128/145'te tam 5 satır | §5.10 |
| Rapor alanlarının yokluğu | 104 raporun **17'sinde `targetPrice` yok**, 18'inde `targetPriceStatus`, 17'sinde `investmentRating` | §7.1: eksik anahtar → NULL |

### 4.2 Çürütülen varsayımlar

| Yanlış varsayım | Gerçek | Kanıt |
|---|---|---|
| `SECTOR_INDUSTY_MAPPING_LC` endüstri anahtarları için kullanılabilir | 145'in **32'si 404** | 7 sektörde ölçüldü |
| yfinance property'leri yanıtın tamamını verir | **23 alan** (11'i iki tam blok) + top-level `key` atılıyor | §4.3 |
| Geçersiz `region` hata üretir | Sessizce **US** döndürüyor | `XX`, `EUROPE`, `''` |
| Sektör ve endüstri `overview`'ları aynı 7 anahtarlı | **Değil**: endüstride `industriesCount` anahtarı ham JSON'da **hiç yok** (6 vs 7). yfinance'in `.get()` çağrısı 7. alanı üretiyor | 156 varlık, ham JSON |
| `Industry.top_companies` her zaman dolu | `infrastructure-operations`'ta **üç blok da yok** | 1/145 |
| `topPerforming` ve `topGrowth` aynı sembol için aynı `ytdReturn` verir | Tam-liste ölçümünde **50 ortak satırın 8'inde farklı** | 24 endüstri, ham JSON |
| Bölge tüm yanıtı kapsar | Yalnız 5 liste bloğunu kapsar | 5 bölge × 2 varlık |
| "All Industries" satırı ada bakarak elenmeli | `key` ve `symbol` alanları **yok**; ada bakmak dile bağlı | 11 sektör |
| `researchReports[].targetPrice` de sarmalı | **Çıplak `float`** (`450.0`), oysa `topCompanies[].targetPrice` sarmalı; ayrıca 104 raporun 17'sinde **hiç yok** | §4.4 |
| `reportTitle` birkaç yüz karakter | Max **23 570** karakter; `TEXT` (65 535 **bayt**) utf8mb4'te taşabilir | 104 rapor |

### 4.3 yfinance'in attığı veri

`Sector`/`Industry` property'leri ile ham yanıtın karşılaştırması:

| Blok | Yahoo'nun döndürdüğü anahtarlar | yfinance'in açığa çıkardığı | Atılan |
|---|---|---|---|
| `performance` | `ytdChangePercent`, `regMarketChangePercent`, `oneYearChangePercent`, `threeYearChangePercent`, `fiveYearChangePercent` | **hiçbiri** | **5** |
| `performanceOverviewBenchmark` | aynı 5 + `name` (`"S&P 500"`) | **hiçbiri** | **6** |
| `topCompanies[]` | `symbol`, `name`, `rating`, `marketWeight`, `marketCap`, `targetPrice`, `lastPrice`, `ytdReturn`, `regMarketChangePercent` | ilk 4 | **5** |
| `topETFs[]` / `topMutualFunds[]` *(yalnız sektör)* | `symbol`, `name`, `netAssets`, `expenseRatio`, `lastPrice`, `ytdReturn` | ilk 2 | **4** |
| `industries[]` | `key`, `name`, `symbol`, `marketWeight`, `ytdReturn`, `regMarketChangePercent` | ilk 4 | **2** + "All Industries" satırı |
| `topPerformingCompanies[]` | `symbol`, `name`, `ytdReturn`, `lastPrice`, `targetPrice` | hepsi | 0 |
| `topGrowthCompanies[]` | `symbol`, `name`, `ytdReturn`, `lastPrice`, `growthEstimate` | `lastPrice` hariç | **1** |
| `researchReports[]` | 9 alan | hepsi (`Domain.research_reports` ham listeyi döndürüyor) | 0 |
| top-level | `key`, `name`, `symbol`, `sectorKey`, `sectorName` | `name`, `symbol`, `sectorKey`, `sectorName` | **1** (`key`) |

Toplam **24 alan**: 5+6+5+4+2+0+1+0+1. Bunların 11'i (`performance` ve
`performanceOverviewBenchmark`) yfinance'te **hiçbir property ile
erişilemeyen** iki tam bloktur. "Tüm data eksiksiz yazılmalı" şartı yalnız
ham JSON ile karşılanabilir.

Endüstri yanıtında `topETFs`/`topMutualFunds` **yoktur** (top-level anahtar
listesiyle doğrulandı), bu yüzden o satırın 4 alanı yalnız sektör tarafında
geçerlidir.

### 4.4 Sarmalayıcı asimetrisi

Yahoo sayısal alanları `{"raw": …, "fmt": …, "longFmt": …}` olarak sarar,
**ama tutarlı biçimde değil**:

| Biçim | Alanlar |
|---|---|
| Sarmalı `dict` | `overview.marketCap`, `overview.marketWeight`, `overview.employeeCount`, `performance.*`, `performanceOverviewBenchmark.*` (`name` hariç), `topCompanies[].{marketCap,targetPrice,lastPrice,ytdReturn,regMarketChangePercent,marketWeight}`, `topETFs[]`/`topMutualFunds[].{netAssets,expenseRatio,lastPrice,ytdReturn}`, `industries[].{marketWeight,ytdReturn,regMarketChangePercent}`, `topPerformingCompanies[]`/`topGrowthCompanies[]`'in tüm sayısal alanları |
| Çıplak `int` | `overview.companiesCount`, `overview.industriesCount` |
| **Çıplak `float`** | **`researchReports[].targetPrice`** |
| **Anahtarın hiç olmaması** | `researchReports[].{targetPrice, targetPriceStatus, investmentRating}` (104 raporun 17/18/17'sinde); `topCompanies[].{name, rating, ytdReturn}`; `industries[]`'in "All Industries" satırında `key` ve `symbol` |
| Çıplak `str` | top-level `key`, `name`, `symbol`, `description`, `messageBoardId`, `sectorKey`, `sectorName`, `rating`, `id`, `provider`, `reportType`, `reportDate`, `reportTitle`, `headHtml`, `targetPriceStatus`, `investmentRating`, `performanceOverviewBenchmark.name` |

`targetPrice` aynı adla **üç** farklı hâlde geliyor: sarmalı (`topCompanies`),
çıplak `float` (`researchReports`) ve **hiç yok** (104 raporun 17'si).
§7.1'deki tek `_unwrap()` + `row.get()` yolu bu yüzden zorunludur; iki ayrı
ayrıştırıcı yazılsaydı biri sessizce `None` yazardı.

### 4.5 İstek paylaşımı ve maliyet

Bir `(anahtar, bölge)` çifti **tek** HTTP isteğidir ve o çiftin tüm
dataset'lerini besler. Bölgesiz dataset'ler **birincil bölgenin** yanıtını
kullanır (veri bölgeden bağımsız ölçüldü), ek istek üretmezler.

```
Birincil bölge : 11 (sektör) + 145 (endüstri) = 156 istek
                 → domain_taxonomy, sector_profile, sector_rankings,
                   industry_profile, industry_rankings
Her ek bölge   : +156 istek (yalnız *_rankings)
Bölge doğrulama: US-dışı her yapılandırılmış bölge için +1 istek;
                 + US yapılandırılmamışsa 1 taban isteği daha (§6.6).
                 US referans sektörü zaten çekiliyorsa önbellekten gelir.
```

| Yapılandırma | Toplam istek | 2 istek/sn'de süre |
|---|---|---|
| `US` (varsayılan) | **156** — prob **çalışmaz** ve gerekmez: US, Yahoo'nun geri düşüş bölgesidir | ~78 sn |
| `US,GB` | 156 + 156 + 1 = **313** | ~2,6 dk |
| `US,GB,DE,JP,TR` | 156×5 + 4 = **784** | ~6,5 dk |
| `GB,DE` (US yok) | 156×2 + 2 + 1 = **315** | ~2,6 dk |

---

## 5. Şema

Tüm tablolar `MYSQL_TABLE_ARGS` (InnoDB, utf8mb4, `utf8mb4_0900_ai_ci`)
kullanır; aşağıda **açıkça belirtilen** collation istisnaları dışında.

### 5.1 `domains` — statik kimlik

Upsert; as-of **değil** (ad, açıklama, sembol ve ebeveyn yılda birkaç kez
değişir).

| Kolon | Tip | Kısıt / not |
|---|---|---|
| `domain_key` | `AsciiKeyType(48)` | **PK**. `ascii_bin`: `TECHNOLOGY` canlıda 404 verdi, anahtarlar büyük/küçük harf duyarlı; `ascii_general_ci` iki anahtarı tek satıra indirirdi. Ölçülen max 37 |
| `domain_type` | `ENUM('sector','industry')` | NOT NULL. PK'da **değil** (§2) |
| `symbol` | `SymbolType()` | NOT NULL, **UNIQUE**, FK→`symbols.symbol` (`ON UPDATE CASCADE ON DELETE RESTRICT`) |
| `parent_key` | `AsciiKeyType(48)` | NULL; FK→`domains.domain_key` (self). Sektörde NULL |
| `name` | `String(64)` | NOT NULL; ölçülen max 40 |
| `description` | `Text` | NULL; ölçülen max 446, 156/156 dolu. **Sektörde** bootstrap yazar; **endüstride** `industries[]` bloğu bu alanı içermez, bu yüzden `industry_profile` yazar (§7.3) — ilk `industry_profile` koşusuna kadar NULL |
| `message_board_id` | `String(32)` | NULL; ölçülen max 15. `description` ile aynı iki kaynaklı yol |
| `first_seen_at` | `TsType()` | NOT NULL; `update_columns` **dışında** (AH§5.4 kuralı) |
| `fetched_at` | `TsType()` | NOT NULL; son doğrulama zamanı |

Kısıtlar:
```sql
CONSTRAINT ck_domains_parent CHECK (domain_type = 'sector' OR parent_key IS NOT NULL)
INDEX ix_domains_type_parent (domain_type, parent_key)
```

`ON DELETE RESTRICT` self-FK'de de geçerlidir: bir sektörü silmek 145
endüstriyi öksüz bırakamaz.

### 5.2 `domain_metrics` — as-of, bölgesiz

**PK `(domain_key, as_of_date)`.** Bölge kolonu **yok**: `overview` ve
`performance` 5 bölgede birebir aynı ölçüldü.

| Kolon | kind → tip | Not |
|---|---|---|
| `domain_key` | `AsciiKeyType(48)` | PK, FK→`domains` |
| `as_of_date` | `Date` | PK |
| `companies_count` | `int` → `INTEGER` | Ölçülen max **1517** (`financial-services`) |
| `industries_count` | `int` → `INTEGER` | NULL; endüstri `overview`'unda anahtar **hiç yok** (§4.1) → `row.get()` → NULL |
| `market_cap` | `big` → `DECIMAL(38,0)` | Ham `int`; 1,23×10⁸ … 2,88×10¹³ |
| `market_weight` | `dec` → `DECIMAL(28,12)` | 1,32×10⁻⁵ … 0,746 |
| `employee_count` | `ubig` → `BIGINT UNSIGNED` | NULL; 18 … 11 895 040 |
| `ytd_change_pct` | `dec` | `performance.ytdChangePercent` |
| `reg_market_change_pct` | `dec` | `performance.regMarketChangePercent` |
| `one_year_change_pct` | `dec` | |
| `three_year_change_pct` | `dec` | |
| `five_year_change_pct` | `dec` | |
| `benchmark_name` | `str64` | `"S&P 500"` |
| `benchmark_ytd_change_pct` | `dec` | |
| `benchmark_reg_market_change_pct` | `dec` | |
| `benchmark_one_year_change_pct` | `dec` | |
| `benchmark_three_year_change_pct` | `dec` | |
| `benchmark_five_year_change_pct` | `dec` | |
| `raw_json` | `RawJsonType()` | NOT NULL. Yanıtın **liste-dışı** kısmı: `key`, `name`, `symbol`, `sectorKey`, `sectorName`, `overview`, `performance`, `performanceOverviewBenchmark` — `nz.canonical_json` ile. Liste blokları **dâhil değildir** (§2: `canonical_json` liste sırasını korur, tam zarf kapıyı her koşuda açardı); onlar §5.3–5.6'da %100 tipli kolonlara gidiyor |
| `fetched_at` | `TsType()` | NOT NULL |

`INDEX ix_domain_metrics_date (as_of_date)` — gün bazlı budama ve raporlama.

### 5.3 `domain_top_companies`

**PK `(domain_key, region, as_of_date, symbol)`.**

`region` `RegionType()` · `symbol` `SymbolType()` (**FK yok**, `is_known`
bayrağı) · `name` `str255` NULL · `rating` `str32` NULL · `market_weight`
`dec` NULL · `market_cap` `big` NULL · `last_price` `dec` NULL ·
`target_price` `dec` NULL · `ytd_return` `dec` NULL ·
`reg_market_change_pct` `dec` NULL · `is_known` `Boolean` NOT NULL
`server_default='0'` · `fetched_at` `TsType()`.

`INDEX ix_domain_top_companies_symbol (symbol)` — PK'nın 4. kolonu olduğu
için "bu şirket hangi sektörlerin ilk 50'sinde" sorgusu aksi halde tam
tarama yapardı.

Sıra (`position`) **saklanmaz**: sıralama ölçütü `market_weight` zaten
kolonda, sıra ondan türetilebilir; saklansaydı sıra-değişimi (11 sektörün
8'inde 15 dk içinde) hash'i her koşuda değiştirirdi.

PK boyutu: 48+8+3+32 karakterlik alanlar utf8mb4/ascii karışımı ile
`(48+1) + (16+1) + 3 + (32+1) = 102` byte (üçü de ascii, biri `DATE`; VARCHAR uzunluk öneki dâhil) — InnoDB'nin 3072 byte sınırının çok
altında.

### 5.4 `domain_top_funds` — yalnız sektör

**PK `(domain_key, region, as_of_date, fund_type, symbol)`.**

`fund_type` `ENUM('etf','mutual_fund')` · `name` `str255` NULL (110 fonun
7'sinde yok) · `net_assets` `big` NULL · `expense_ratio` `dec` NULL ·
`last_price` `dec` NULL · `ytd_return` `dec` NULL · `is_known` `Boolean` ·
`fetched_at`.

`symbol` ticker olmayabilir (`0P0001WO1I` — Morningstar kimliği); `is_known`
bunu işaretler.

### 5.5 `domain_top_movers` — yalnız endüstri

**PK `(domain_key, region, as_of_date, rank_type, symbol)`.**

`rank_type` `ENUM('performing','growth')` · `name` `str255` NULL ·
`ytd_return` `dec` NULL · `last_price` `dec` NULL · `target_price` `dec`
NULL (yalnız `performing`) · `growth_estimate` `dec` NULL (yalnız `growth`)
· `is_known` `Boolean` · `fetched_at`.

`rank_type`'ın PK'da olması **veri kaybını önler**, sadece çakışmayı değil:
tam-liste ölçümünde 50 ortak sembolün 8'inde iki uç farklı `ytdReturn`
bildiriyor (§4.1).

### 5.6 `domain_research_reports` + `domain_report_links`

`domain_research_reports` — **PK `(report_id)`**, `AsciiKeyType(64)`
(ölçülen max 50):

`as_of_date` `Date` NOT NULL (**PK'da değil** — "en son görüldüğü gün") ·
`provider` `str64` · `report_type` `str64` · `head_html` `str255` (max 59) ·
`report_title` **`MEDIUMTEXT`** (ölçülen max **23 570** karakter; `TEXT` 65 535 **bayt**tır ve utf8mb4'te 4 baytlık karakterlerle taşabilir) · `target_price` `dec` NULL ·
`target_price_status` `str32` NULL · `investment_rating` `str32` NULL ·
`report_ts_utc` `TsType()` NOT NULL · `first_seen_at` `TsType()` NOT NULL
(`update_columns` dışında) · `fetched_at` `TsType()`.

`as_of_date`'in PK'da olmaması **bilinçlidir**: rapor içeriği değişmez, yalnız
hangi gün hangi domain'de göründüğü değişir ve onu `domain_report_links`
taşır. Kolonun **var olması** ise zorunludur: `AsOfGate` kapı satırının
`as_of_date`'ini `result.writes`'ın ilk satırından okur (§6.2) ve bu tablo
ilk sırada gelebilir — kolon olmasaydı `KeyError` verirdi.

`first_seen_at` bu spec'le birlikte `VOLATILE_COLUMNS`'a eklenir (§6.2/3);
aksi halde her koşuda değişen bir alan hash gövdesine girer ve kapı **asla**
eşitlenmezdi. Bugün hiçbir veri tablosunda `first_seen_at` kolonu olmadığı
için bu ekleme mevcut 13 as-of dataset'inin hash'ini değiştirmez.

`domain_report_links` — **PK `(domain_key, as_of_date, report_id)`**:
`position` `int` NOT NULL · `fetched_at`. FK'ler: `domain_key`→`domains`,
`report_id`→`domain_research_reports` (`ON DELETE CASCADE`).

Bölge kolonu **yok**: rapor kimlikleri 5 bölgede aynı sırayla döndü.

### 5.7 `domain_asof_state` — kapı

**PK `(domain_key, dataset, region)`.** Kolonlar `asof_state` ile birebir
aynı: `as_of_date` `Date` NOT NULL · `content_hash` `HashType()` NOT NULL ·
`row_count` `Integer` NOT NULL · `first_seen_at` `TsType()` NOT NULL
(`update_columns` dışında) · `fetched_at` `TsType()` NOT NULL.

`domain_key` `AsciiKeyType(48)` (FK→`domains`) · `dataset` `AsciiKeyType(32)` · `region` `RegionType()` NOT NULL;
bölgesiz dataset'ler `'*'` yazar (`market_runner.GLOBAL_SCOPE_MARKER`
deseni). `INDEX ix_domain_asof_dataset_date (dataset, as_of_date)`.

**Neden `asof_state` kullanılmıyor:** oradaki anahtar `(symbol, dataset)`
ve `AsOfDataset._gate_write` kapı sembolünü `row["symbol"]`'den okuyor.
Domain tablolarındaki `symbol` **şirketin** sembolüdür; kapı yanlış varlığa
yazılırdı. Ayrıca bölge ekseni oraya sığmaz. Bu, `asof_base.py`'nin
`SnapshotDataset`/`HashGatedDataset`'i neden kullanmadığını açıklayan
gerekçenin aynısıdır.

### 5.8 `symbols`'a eklenen 156 satır

Şema değişikliği **yok**. `domain_taxonomy` şu kolonları yazar:

| Kolon | Değer | Kaynak |
|---|---|---|
| `symbol` | `^YH311`, `^YH31130020`, … | Yanıtın `symbol` alanı |
| `short_name` | Domain adı (`Technology`) | Yanıtın `name` alanı |
| `quote_type` | `INDEX` | 6/6 domain sembolünde ölçüldü |
| `exchange` | `YHD` | 6/6 |
| `currency` | `USD` | 6/6 |
| `timezone` | `America/New_York` | 6/6 |
| `is_active` | `0` | **Yalnız INSERT'te** (§7.8) |
| `last_seen_at` | `fetched_at` | |

`update_columns` **`is_active` ve `unknown_streak` içermez**: kullanıcı
`^YH311`'i elle etkinleştirmişse bir sonraki domain sync onu geri kapatmaz.

`is_active=0` olduğu için varsayılan `yfin sync` bu sembolleri çekmez;
`--include-inactive` ya da `--quote-type INDEX` ile sektör/endüstri
indekslerinin fiyat geçmişi, `info` ve `fast_info` verisi mevcut
dataset'lerle toplanabilir (`^YH311.history(period='5d')` → `(5,7)`
ölçüldü).

### 5.9 `sync_run_items`'a bir kolon

`region` `RegionType()` NULL (`ALGORITHM=INSTANT`). Domain hücreleri
doldurur; `symbol` alanına **domain sembolü** yazılır (`^YH31130020`),
`domain_key` değil — kolon `VARCHAR(32)` ve 5 endüstri anahtarı bunu aşıyor
(en uzun 37: `utilities-independent-power-producers`).

`market_runner`'ın bölgeyi `symbol` alanına yazan mevcut davranışı
**değiştirilmez**: değiştirmek mevcut denetim sorgularını kırardı ve bu
spec'in amacına hizmet etmez. Bilinçli, belgelenmiş bir tutarsızlıktır.

### 5.10 Tablo özeti

| Tablo | PK | Satır tahmini (US, 1 yıl) |
|---|---|---|
| `domains` | `(domain_key)` | 156 |
| `domain_metrics` | `(domain_key, as_of_date)` | 156 × 365 ≈ **57 000** |
| `domain_top_companies` | `(domain_key, region, as_of_date, symbol)` | (550 + **2743**) × 365 ≈ **1 202 000** (endüstri ortalaması ölçülen 19,05) |
| `domain_top_funds` | `(domain_key, region, as_of_date, fund_type, symbol)` | 220 × 365 ≈ **80 000** |
| `domain_top_movers` | `(domain_key, region, as_of_date, rank_type, symbol)` | **1370** × 365 ≈ **500 000** (ölçülen 685 performing + 685 growth) |
| `domain_research_reports` | `(report_id)` | 624 satır/gün → **516 tekil**/gün, günler arası tekilleşerek ≈ **150 000** |
| `domain_report_links` | `(domain_key, as_of_date, report_id)` | 624 × 365 ≈ **228 000** |
| `domain_asof_state` | `(domain_key, dataset, region)` | Her domain **2** as-of dataset'ine girer (profil + rankings): 156×2 = **312**, çarpı bölge sayısı yalnız rankings için |

Toplam ~2,2 milyon satır/yıl (tek bölge). Budama §11'de.

### 5.11 Yazma modu ve silme kapsamı

Her `TableWrite`'ın modu ve kapsamı **açıkça** tanımlıdır. `domain_top_companies`
**iki farklı dataset** tarafından yazıldığı için bu tablo kritiktir: kapsam
`domain_key` içermeseydi `industry_rankings` sektör satırlarını silerdi.

| Tablo | `mode` | `scope_columns` | `update_columns` | Gerekçe |
|---|---|---|---|---|
| `symbols` | `upsert` | — | `short_name`, `quote_type`, `exchange`, `currency`, `timezone`, `last_seen_at` | `is_active`/`unknown_streak` dışarıda (§7.8) |
| `domains` | `upsert` | — | `symbol`, `parent_key`, `name`, `fetched_at` *(bootstrap)*; `description`, `message_board_id`, `fetched_at` *(industry_profile)* | `first_seen_at` dışarıda (AH§5.4). İki yazıcı **ayrık kolon kümeleri** günceller, birbirini ezmez |
| `domain_metrics` | `replace_scope` | `("domain_key", "as_of_date")` | tüm veri kolonları + `fetched_at` | Aynı gün yeniden koşulursa tek satır kalır |
| `domain_top_companies` | `replace_scope` | `("domain_key", "region", "as_of_date")` | tüm veri kolonları + `is_known`, `fetched_at` | **`domain_key` kapsamda olmak zorunda**: iki dataset aynı tabloya yazıyor. Yahoo listeden bir şirket çıkardığında eski satır kalmasın diye `replace_scope` (`fund_top_holdings` deseni, `funds.py:426-433`) |
| `domain_top_funds` | `replace_scope` | `("domain_key", "region", "as_of_date")` | aynı | |
| `domain_top_movers` | `replace_scope` | `("domain_key", "region", "as_of_date")` | aynı | `rank_type` kapsamda **değil**: iki liste tek fetch'ten gelir, birlikte yazılır |
| `domain_research_reports` | `upsert` | — | `provider`, `report_type`, `head_html`, `report_title`, `target_price`, `target_price_status`, `investment_rating`, `report_ts_utc`, `as_of_date`, `fetched_at` | `first_seen_at` dışarıda. **`replace_scope` OLAMAZ**: tablo paylaşımlıdır, kapsam kolonu yoktur |
| `domain_report_links` | `replace_scope` | `("domain_key", "as_of_date")` | `position`, `fetched_at` | |
| `domain_asof_state` | `upsert` | — | §6.2'deki kapı kuralı | |

`scope_values` gerekmez: her yazımın kapsamı satırlardan türetilebilir —
tek istisna tamamen boş sonuç, ki orada `AsOfDataset.upsert`'ün `is_empty`
dalı zaten hiçbir şey yazmaz (§7.5).

---

## 6. Sözleşme değişiklikleri

### 6.1 `DomainDataset` ve `DomainContext`

`GlobalDataset`'in kardeşi. `SyncContext`'ten **türemez**: `symbol` alanı
domain tarafında yanlış anlam taşırdı — ve burada bu, `market/base.py`'deki
gerekçeden daha keskindir, çünkü domain tablolarında `symbol` **gerçekten
vardır** ama şirketin sembolüdür.

```python
DomainType = Literal["sector", "industry"]

@dataclass
class DomainContext:
    fetched_at: datetime
    as_of_date: date
    primary_region: str
    region: str = "*"                    # "*" = bolgesiz tur
    key: str | None = None
    domain_type: DomainType | None = None
    _cache: dict[str, Any] = field(default_factory=dict, repr=False)

    def cached(self, key: str, fn: Callable[[], Any]) -> Any: ...
    def for_target(self, key: str, domain_type: DomainType) -> DomainContext: ...
    def for_region(self, region: str) -> DomainContext: ...

class DomainDataset[RawT](ABC):
    name: str
    depends_on: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    scope: DomainType                    # hangi anahtar kumesi uzerinde doner
    regional: bool = False               # True ise bolge dongusune girer

    @abstractmethod
    def fetch(self, ctx: DomainContext) -> RawT: ...
    @abstractmethod
    def normalize(self, raw: RawT, key: str) -> NormalizedResult: ...
    def upsert(self, writer: RowWriter, result: NormalizedResult) -> WriteStats: ...
```

`for_target` ve `for_region` **aynı `_cache`'i paylaşır** (`MarketContext.for_region`
deseni): bir `(anahtar, bölge)` çiftinin ham JSON'u bir kez çekilir ve o
çiftin tüm dataset'lerini besler.

### 6.2 `AsOfGate` mixin'i — as-of kapısının `Dataset`'ten ayrılması

**İki kanca yetmez.** `asof_base.py`'nin bugünkü hâli dört yerde domain
tarafında karşılığı olmayan varsayımlar yapıyor:

| Yer | Varsayım | Domain'de neden tutmaz |
|---|---|---|
| `asof_base.py:88` | `first["symbol"]` | `domain_metrics`'te `symbol` kolonu **yok**; `domain_top_companies`'te var ama o **şirketin** sembolü |
| `asof_base.py:95` | `first["as_of_date"]` | Yazılan her tabloda `as_of_date` olmak zorunda (§5.6'ya bu yüzden eklendi) |
| `asof_base.py:114` | `GATE_TABLE` modül sabiti | Kapı `domain_asof_state` |
| `asof_base.py:59` | `AsOfDataset(Dataset[RawT])` — `fetch(SyncContext)`, `normalize(raw, symbol)` | `DomainDataset` ayrı bir ABC: `fetch(DomainContext)`, `normalize(raw, key)`. İki hiyerarşi **birleştirilemez** |

Sonuncusu belirleyicidir: `DomainAsOfDataset`, `AsOfDataset`'ten
**türeyemez**. Bu yüzden kapı mantığı `Dataset`'e bağımlı olmayan bir
mixin'e çıkarılır ve iki taraf da onu kullanır.

```python
class AsOfGate:
    # as-of kapisi -- Dataset hiyerarsisinden BAGIMSIZ.
    # AsOfDataset ve DomainAsOfDataset bunu paylasir; ne fetch ne normalize
    # imzasina dokunur, yalnizca content_hash + upsert saglar.
    name: str                                          # alt sinif saglar
    asof_gate_table: str = GATE_TABLE                  # "asof_state"
    asof_gate_key_columns: tuple[str, ...] = GATE_KEY_COLUMNS

    def gate_identity(self, result: NormalizedResult) -> dict[str, Any]:
        # Kapi satirinin ANAHTAR alanlari. Varsayilan = bugunku davranis.
        first = _first_row(result)
        return {"symbol": first["symbol"], "dataset": self.name}

    def content_hash(self, result) -> str: ...      # bugunku govde, degismez
    def upsert(self, writer, result) -> WriteStats: ...
        # bugunku govde; tek fark:
        #   GATE_TABLE                    -> self.asof_gate_table
        #   {"symbol": …, "dataset": …}   -> self.gate_identity(result)


class AsOfDataset[RawT](AsOfGate, Dataset[RawT]):
    ...   # sembol tarafi -- davranisi BIREBIR bugunku hali


class DomainAsOfDataset[RawT](AsOfGate, DomainDataset[RawT]):
    asof_gate_table = "domain_asof_state"
    asof_gate_key_columns = ("domain_key", "dataset", "region")

    def gate_identity(self, result):
        first = _first_row(result)
        return {"domain_key": first["domain_key"],
                "dataset": self.name, "region": self.region}
```

`region`, dataset örneğinde **durum olarak tutulmaz** (registry'deki
dataset'ler tekildir); tur başına `normalize()`'a `DomainContext`'ten geçen
değer satırlara yazılır ve `gate_identity` onu `first["region"]`'den okur.
Bölgesiz dataset'lerde satırlarda `region` kolonu yoktur; o durumda
`gate_identity` `"*"` döndürür.

**Ad seçimi:** çıplak `gate_table` **kullanılamaz** — `HashGatedDataset` o
adı zaten **farklı** bir anlamda kullanıyor (orada kapı bir VERİ tablosudur,
`hash_gated.py:27-33`). Kardeş sınıflarda aynı ad, farklı sözleşme sessiz
bir tuzak olurdu.

**`VOLATILE_COLUMNS`'a `first_seen_at` eklenir.** Bugün hiçbir **veri**
tablosunda bu kolon yok (`models/asof.py:42` — yalnız `asof_state`), bu
yüzden mevcut 13 dataset'in hash'i değişmez. Eklenmezse
`domain_research_reports.first_seen_at` her koşuda değişip hash gövdesine
girer ve kapı **asla** eşitlenmez — mekanizma sessizce hiç çalışmazdı.

**`is_known` hash'te KALIR.** `funds.py:432-437` bunu açıkça
gerekçelendiriyor: "bayrak hash gövdesine girer, böylece evren değiştiğinde
kapı açılır ve satırlar güncellenir". Dışlansaydı kullanıcı `SGE.L`'i
`symbols`'a ekledikten sonra `is_known` **hiç** güncellenmezdi — kapı
`skipped` derdi.

`asof_produces()` yardımcısı `gate` parametresi alacak biçimde genişletilir;
varsayılanı yine `GATE_TABLE`.

### 6.3 Üçüncü registry

```python
DOMAIN_DATASETS: Registry[DomainDataset[Any]] = Registry(
    bootstrap="domain_taxonomy",
    aliases={
        "sector":   ("sector_profile", "sector_rankings"),
        "industry": ("industry_profile", "industry_rankings"),
    },
)
```

`Registry` sınıfı **değişmez**; `Registrable` protokolü `name` +
`depends_on` istiyor, `DomainDataset` ikisini de taşıyor.

### 6.4 Ham JSON çekimi

```python
_QUERY = "https://query1.finance.yahoo.com/v1/finance"

def fetch_domain(key: str, domain_type: DomainType, region: str) -> dict[str, Any]:
    path = "sectors" if domain_type == "sector" else "industries"
    params = {"formatted": "true", "withReturns": "true",
              "lang": "en-US", "region": region}
    payload = call_yahoo(
        lambda: YfData().get_raw_json(f"{_QUERY}/{path}/{key}", params=params),
        what=f"{domain_type}:{key}:{region}",
    )
    return payload["data"]
```

`YfData` yfinance'in kendi veri katmanıdır: `yf.config.network.proxy`,
curl_cffi oturumu, cookie/crumb yönetimi ve `P§6.1`'deki proxy kurulumu
**aynen** geçerlidir. `call_optional` **kullanılmaz** (§8.2).

`payload["data"]` yokluğu `KeyError('data')` fırlatır ve
`classify_error` → `DATA` → `failed` olur; boş anahtar bu yoldan yakalanır.

### 6.5 Anahtar evreni

```python
# Kendi sabitimiz. "Sektorleri listele" ucu YOKTUR; 11/11 canli
# dogrulandi. `test_domain_key_source.py` bu kumenin
# SECTOR_INDUSTY_MAPPING_LC'nin ust duzey anahtarlariyla ayni oldugunu
# surer -- kutuphane degisirse sessizce sapmayiz.
SECTOR_KEYS: tuple[str, ...] = (
    "basic-materials", "communication-services", "consumer-cyclical",
    "consumer-defensive", "energy", "financial-services", "healthcare",
    "industrials", "real-estate", "technology", "utilities",
)
```

Endüstri anahtarları **yalnız** sektör yanıtının `industries[].key`
alanından gelir. `SECTOR_INDUSTY_MAPPING_LC`'nin endüstri listeleri
**hiçbir yerde import edilmez**; §9.1'deki test bunu kalıcı kılar.

Runner, `domain_taxonomy` turundan sonra anahtarları **DB'den** okur:
```sql
SELECT domain_key, symbol FROM domains WHERE domain_type = 'industry'
```
Bootstrap her çözümlemede başa eklendiği için liste her zaman tazedir.
Bellekte taşınmaması, `--datasets industry_profile` gibi kısmi koşularda da
aynı yolu kullanmasını sağlar.

### 6.6 Bölge doğrulaması

Üç tuzak var ve üçü de ölçülmüş:

1. Geçersiz bölge **sessizce US** döndürüyor (`XX`, `EUROPE`, `''`, `us`).
2. Taban olarak **birincil bölge** alınırsa, birincil bölgenin kendisi
   geçersizse (`YF_DOMAIN_REGIONS=XX`) hiçbir şey yakalanmaz. Taban
   **her zaman `US`** olmalıdır: Yahoo'nun geri düşüş bölgesi odur.
3. **Liste eşitliği kullanılamaz.** `topCompanies` sırası 15 dakikada 11
   sektörün 8'inde, `technology`'de **kümesi bile** değişti. İki ardışık
   istek arasında liste kayarsa geçersiz bir bölge doğrulamayı **geçer** —
   tam olarak probun engellemek için var olduğu senaryo.

```python
US = "US"
FALLBACK_OVERLAP = 0.5     # olculdu: GB/DE/JP/TR ∩ US = 0

def domain_regions(settings) -> list[str]:
    # Bicim + AMPIRIK dogrulama. Yahoo gecersiz bolgeyi SESSIZCE US'e
    # dusuruyor; bicim kontrolu tek basina 'XX'i gecirir ve US verisi
    # XX etiketiyle yazilirdi.
    regions = [r.strip().upper()
               for r in settings.yf_domain_regions.split(",") if r.strip()]
    bad = [r for r in regions if not re.fullmatch(r"[A-Z]{2}", r)]
    if bad:
        raise ValueError(f"gecersiz bolge kodu (ISO 3166-1 alpha-2): {bad}")

    candidates = [r for r in regions if r != US]
    if not candidates:
        return regions                      # yalniz US: dogrulanacak sey yok

    ref = settings.yf_domain_reference_sector
    base = {c["symbol"] for c in fetch_domain(ref, "sector", US)["topCompanies"]}
    for region in candidates:
        probe = {c["symbol"]
                 for c in fetch_domain(ref, "sector", region)["topCompanies"]}
        overlap = len(probe & base) / len(base) if base else 0.0
        if overlap >= FALLBACK_OVERLAP:
            raise ValueError(
                f"bolge {region!r} Yahoo tarafindan desteklenmiyor: "
                f"referans sektorun sirket listesi US ile %{overlap:.0%} ortusuyor "
                f"(esik %{FALLBACK_OVERLAP:.0%}) -- US verisi {region!r} "
                f"etiketiyle yazilacakti"
            )
    return regions
```

**Neden küme kesişimi, eşitlik değil:** ölçüm, desteklenen bölgelerin US ile
kesişiminin **tam olarak 0** olduğunu gösteriyor (GB, DE, JP, TR — dördünde
de). Geri düşüş durumunda kesişim **1,0**'dır. %50 eşiği iki durumu ayırmak
için fazlasıyla geniş bir pay bırakır ve sıra/küme kaymasından etkilenmez.

**Maliyet:** US-dışı yapılandırılmış bölge başına 1 istek, artı US
yapılandırılmamışsa 1 taban isteği. `US` tek başınayken **sıfır** ek istek —
ve koruma da gerekmez, çünkü US geri düşüşün kendisidir.

**Sıra:** `open_run`'dan **önce** (hatalı yapılandırma `running` durumunda
bir `sync_runs` satırı bırakmasın), ama **proxy kurulumundan sonra** —
`market_runner`'da `_setup_proxy` de `open_run`'dan önce çağrılıyor
(`market_runner.py:163-172`); prob aksi halde doğrudan bağlantıdan giderdi
ve havuz politikası dışında kalırdı.

Taban isteğinin yanıtı `DomainContext._cache`'e `raw:{ref}:US` anahtarıyla
konur; `US` yapılandırılmışsa `sector_profile`/`sector_rankings` onu
yeniden çekmez.

### 6.7 `domain_runner.py`

`market_runner.py`'nin yapısı. Kilit `yfin_domain_sync`; `yfin_sync` ve
`yfin_market_sync` ile çakışmaz, üç komut eş zamanlı koşabilir.

**Transaction sınırı = tur = (dataset × anahtar × bölge).** Bozuk bir
endüstri diğer 144'ü düşürmez. Tek istisna `domain_taxonomy`: 156 `symbols`
+ 156 `domains` satırı **tek turda, tek transaction'da** yazılır —
taksonomi ya bütün olarak tutarlıdır ya hiç.

**Sıra bağlayıcıdır:**

1. `_setup_proxy()` — havuzdan tek proxy, `configure_yfinance` (`market_runner.py:163-172` ile aynı sıra)
2. `domain_regions()` — bölge doğrulaması; proxy'den **sonra**, `open_run`'dan **önce**
3. `open_run(scope=RunScope.DOMAIN, symbol_count=0, dataset_count=len(datasets))`
4. `domain_taxonomy` turu — `symbols` yazımı `domains.symbol` FK'sinden
   önce, `domains` yazımı diğer tüm tabloların FK'sinden önce
   (`NormalizedResult.writes` sırası `apply_write` tarafından korunur);
   `domains` **içindeki** satır sırası da bağlayıcıdır (§7.2)
5. Endüstri anahtarları DB'den okunur
6. `scope='sector'` dataset'leri, sonra `scope='industry'`
7. Her dataset için: `regional=False` → tek tur (`region='*'`);
   `regional=True` → bölge başına bir tur

`NormalizedResult.writes` içinde `domain_research_reports`,
`domain_report_links`'ten **önce** gelir (FK).

Proxy: `market_runner._setup_proxy` ile aynı politika — havuzdan tek proxy,
yoksa doğrudan bağlantı; parolası çözülemeyen proxy dead yapılmaz, atlanır.

---

## 7. Dataset'ler

### 7.1 Ortak: `_unwrap`

```python
def _unwrap(value: Any) -> Any:
    """Yahoo'nun {"raw":…,"fmt":…} sarmalayicisini acar.

    Ayni alan iki blokta iki farkli bicimde gelebiliyor:
    topCompanies[].targetPrice SARMALI, researchReports[].targetPrice
    CIPLAK float (S4.4). Iki ayri ayristirici yazilsaydi biri sessizce
    None yazardi.
    """
    if isinstance(value, Mapping):
        return value.get("raw")
    return value
```

Eksik anahtar `row.get(name)` → `None` → ilgili `kind`'ın dönüştürücüsü →
NULL. **Ham JSON pandas'a hiç uğramadığı için `NaN` sorunu yoktur**:
yfinance yolunda 12 satırda `name` alanı `NaN` (float) geliyordu, bunun tek
nedeni `pd.DataFrame` kurulumunun eksik anahtarı `NaN`'a çevirmesiydi.
`_scrub_nan` yalnız `raw_json` yolunda devrede kalır.

Boş dize (`''`) NULL'a çevrilir (AH'nin `priceTargetAction` kuralı).

### 7.2 `domain_taxonomy` — bootstrap, bölgesiz

- **scope:** `sector`, **regional:** `False`
- **fetch:** 11 sektör, birincil bölge (`ctx.cached`)
- **produces:** `("symbols", "domains")`
- **as-of değil:** düz upsert

Her sektör yanıtından:
- 1 `domains` satırı (`domain_type='sector'`, `parent_key=NULL`)
- `industries[]`'in **`key` alanı olan** her satırından 1 `domains` satırı
  (`domain_type='industry'`, `parent_key=<sektör>`)
- Her ikisi için 1 `symbols` satırı

**"All Industries" eleme kuralı:** `key` alanının yokluğu. yfinance
`if i.get('name') != 'All Industries'` ile eliyor; bu **ada** bakan, dile
bağlı bir kural. Ölçüm: 13 satırın 12'sinde `key` ve `symbol` var, o satırda
ikisi de yok. `lang=en-US` bugün sabit olsa bile ada bakan bir eşleşme,
Yahoo etiketi değiştirdiğinde 13. satırı `key=NULL` ile PK'ya sokmaya
çalışırdı. Elenen satırın verisi kaybolmaz: değerleri `performance`
bloğuyla özdeş (§1 kapsam dışı tablosu).

`industries[]` satırının `name`/`symbol` alanları `domains`'e yazılır;
`marketWeight`/`ytdReturn`/`regMarketChangePercent` **yazılmaz** (o
endüstrinin kendi `domain_metrics` satırıdır). `description` ve
`message_board_id` bu blokta **yoktur**; endüstri için onları
`industry_profile` yazar (§7.3).

**Satır sırası tek `TableWrite` içinde bağlayıcıdır:** `parent_key` bir
self-FK'dir, bu yüzden bir sektörün satırı kendi endüstrilerinden **önce**
gelmelidir. `apply_write` satırları verilen sırayla gönderir; üretim
döngüsü de sektör → o sektörün endüstrileri sırasını korur. (Tablo sırası
`symbols` → `domains`; satır sırası `domains` **içinde**.)

`domains` yazımının kapsamı §5.11'de: `update_columns` `first_seen_at`
içermez, ve bootstrap ile `industry_profile` **ayrık kolon kümeleri**
günceller.

### 7.3 `sector_profile` / `industry_profile` — as-of, bölgesiz

- **regional:** `False`; **fetch:** `ctx.cached(f"raw:{key}:{primary_region}")`
- **produces** (`sector_profile`): `("domain_metrics",
  "domain_research_reports", "domain_report_links", "domain_asof_state")`
- **produces** (`industry_profile`): yukarıdakiler **+ `"domains"`** (§7.3'ün
  `description`/`message_board_id` yazımı)
- **taban:** `DomainAsOfDataset`

`overview` + `performance` + `performanceOverviewBenchmark` → 1
`domain_metrics` satırı; `raw_json` yanıtın **liste-dışı** kısmıdır (§5.2).
`researchReports[]` → 4 `domain_research_reports` (upsert) + 4
`domain_report_links` satırı; `position` liste sırasıdır.

`industry_profile` **`domains` satırını da yazar**, ama yalnız
`description` + `message_board_id` kolonlarını: bu iki alan `industries[]`
bloğunda yoktur (§4.3), dolayısıyla bootstrap onları endüstriler için
dolduramaz. Kimlik alanlarına (`symbol`, `parent_key`, `name`,
`domain_type`) **dokunmaz** — onlar bootstrap'in işidir ve §5.11'deki
ayrık `update_columns` bunu zorlar. Ayrıca `sectorKey`'i
`domains.parent_key` ile karşılaştırıp uyuşmazlıkta `WARNING` üretir
(taksonomi kayması sinyali).

**`writes` sırası bağlayıcıdır:**
`domain_metrics` → `domain_research_reports` → `domain_report_links` →
`domains`.

- Son ikisi arasındaki sıra bir **FK** zorunluluğudur
  (`domain_report_links.report_id` → `domain_research_reports`).
- `domains` **en sona** konur ve bunun FK ile ilgisi yoktur: o satır
  bootstrap tarafından **zaten yazılmıştır**, `industry_profile` yalnız iki
  kolonu günceller. Sona konmasının nedeni `AsOfGate`'in kapı satırının
  `as_of_date`/`fetched_at`'ini `writes`'ın **ilk satırından** okumasıdır
  (`asof_base.py:88-96`); `domains` satırında `as_of_date` kolonu **yoktur**,
  başta olsaydı `KeyError` verirdi.

Aynı nedenle `domain_research_reports` de `as_of_date` taşır (§5.6):
sıra ileride değişirse kapı `KeyError` yerine doğru değeri okur. Tek
`as_of_date`'siz tablo `domains` kalır ve o hep sondadır.

`domain_metrics` ve rapor tabloları aynı `DomainAsOfDataset` kapısındadır
(`dataset='sector_profile'` ya da `'industry_profile'`, `region='*'`). İki
profil dataset'i **aynı** tablolara yazar ama kapı satırları `dataset`
değeriyle ayrışır; `domain_key` zaten sektör ve endüstri arasında ayrık.

### 7.4 `sector_rankings` — as-of, bölgeli

- **regional:** `True`; **produces:** `("domain_top_companies",
  "domain_top_funds", "domain_asof_state")`

`topCompanies[]` → `domain_top_companies`; `topETFs[]` →
`domain_top_funds` (`fund_type='etf'`); `topMutualFunds[]` →
`fund_type='mutual_fund'`.

`is_known` **upsert içinde**, kapı çalışmadan **önce** DB'den doldurulur
(`funds.py:414-446` deseni; `writer.known_symbols` `persistence.py:67`'de
`RowWriter` protokolünde zaten var, ekleme gerekmez):

```python
def upsert(self, writer, result):
    writes = [_mark_known(writer, w) if w.rows and "is_known" in w.update_columns
              else w for w in result.writes]
    return super().upsert(writer, NormalizedResult(writes=writes,
                                                   skipped=dict(result.skipped)))
```

Sıra bağlayıcıdır ve `is_known` hash gövdesine **girer** — `funds.py:432-437`
bunu açıkça gerekçelendiriyor: bayrak hash'te olduğu için evren
değiştiğinde (kullanıcı `SGE.L`'i `symbols`'a eklediğinde) kapı açılır ve
satırlar güncellenir. Dışlansaydı bayrak `0`'da donup kalırdı.

Satırlar hash'ten önce `key_columns`'a göre **sıralanır**
(`AsOfGate.content_hash`'in mevcut kuralı). Zorunlu: `topCompanies`
sırası 15 dakika arayla 11 sektörün 8'inde değişti (`technology`'de küme
de değişti — orada hash'in değişmesi **doğrudur**, veri gerçekten
değişmiştir).

### 7.5 `industry_rankings` — as-of, bölgeli

- **regional:** `True`; **produces:** `("domain_top_movers",
  "domain_top_companies", "domain_asof_state")`

`topCompanies[]` → `domain_top_companies` (sektörle **aynı tablo**);
`topPerformingCompanies[]` → `rank_type='performing'`;
`topGrowthCompanies[]` → `rank_type='growth'`.

Endüstri yanıtında `topETFs`/`topMutualFunds` **yoktur** (§4.1), bu yüzden
`domain_top_funds` yalnız sektör tarafında dolar — sessiz veri kaybı değil,
ölçülmüş bir yokluk.

`infrastructure-operations`: **üç** bloğun hiçbiri yok → `NormalizedResult`
boş → `AsOfGate.upsert`'ün `is_empty` dalı kapı satırını **yazmaz**
(AH§6.1/3: aksi halde her boş varlık için ölü satır birikir ve
`first_seen_at` "ilk kez boş dönüldü" anlamına kayardı) ve `_record_items`
hücreleri `empty` yapar.

### 7.6 Eşlenmemiş anahtar uyarısı

`_MAPPED_OVERVIEW_KEYS` **iki varyant** tanır: sektörde 7 anahtar
(`industriesCount` dâhil), endüstride 6 (anahtar ham JSON'da **hiç yok**,
§4.1). Tek küme kullanılsaydı her endüstri "eksik anahtar" ya da her sektör
"fazla anahtar" uyarısı üretirdi.

Her blok için bilinen anahtar kümesi (`_MAPPED_OVERVIEW_KEYS`,
`_MAPPED_COMPANY_KEYS`, `_MAPPED_FUND_KEYS`, `_MAPPED_MOVER_KEYS`,
`_MAPPED_REPORT_KEYS`, `_MAPPED_PERFORMANCE_KEYS`) — ölçülen kümeler §4.3'te.
Küme dışında bir anahtar gelirse `WARNING` ile adı loglanır
(`market/status.py`'deki `_MAPPED_SUMMARY_KEYS` deseni). Veri kaybı olmaz
(`raw_json`'da durur), log tipli kolona terfi sinyalidir.

### 7.7 `as_of_date`

```python
NY = ZoneInfo("America/New_York")
as_of_date = fetched_at.replace(tzinfo=UTC).astimezone(NY).date()
```

6/6 domain sembolünün timezone'u `America/New_York`, para birimi `USD`,
benchmark `S&P 500` — bu bir ABD piyasa toplamıdır. UTC günü kullanılsaydı
23:30 UTC ve 00:30 UTC koşuları aynı işlem günü için **iki satır** üretirdi
ve PK bunu ayırt edemezdi.

### 7.8 `symbols` yazımı

```python
TableWrite(
    table="symbols",
    rows=symbol_rows,           # is_active=0 ACIKCA verilir (server_default '1')
    key_columns=("symbol",),
    update_columns=("short_name", "quote_type", "exchange",
                    "currency", "timezone", "last_seen_at"),
)
```

`is_active` ve `unknown_streak` `update_columns` **dışındadır**: yalnız
INSERT'te uygulanır, mevcut satırların değeri korunur.

---

## 8. Hata sınıflandırması ve denetim

### 8.1 Durum türetmesi

Durum türetme **kuralları** değişmez; ama üç fonksiyonun da **imzası
genişler**, çünkü `sync_run_items.region`'ı (§5.9) dolduran tek yol
budur — `ItemRecord`'da bugün böyle bir alan yok (`runner.py:56-66`):

| Değişiklik | Yer | Mevcut davranışa etkisi |
|---|---|---|
| `ItemRecord.region: str \| None = None` | `runner.py:56-66` | Yok (varsayılan `None`) |
| `_record_items(..., region: str \| None = None)` | `runner.py:331` | Yok |
| `_failed_records(..., region=None)` / `_skipped_records(..., region=None)` | `runner.py:379`, `:407` | Yok |
| `write_items` `region`'ı `SyncRunItem`'a taşır | `runner.py:612-643` | Yok (NULL yazar) |

`_failed_records`/`_skipped_records` zaten registry parametresi alıyor;
`_record_items` **almıyor** (`DatasetMeta` + `symbol` alıyor) — spec'in ilk
taslağındaki "üçü de registry parametreli" ifadesi yanlıştı.
`DOMAIN_DATASETS` yalnız ilk ikisine geçilir.

| Durum | Koşul |
|---|---|
| `ok` | `verified == attempted`, `attempted > 0` |
| `empty` | `attempted == 0 && skipped == 0` — blok yok (`infrastructure-operations`), bölgede liste boş (GB `topETFs`), ya da tablo o dataset için hiç geçerli değil |
| `skipped` | `attempted == 0 && skipped > 0` — `content_hash` değişmedi |
| `failed` | `verified != attempted`, ya da fetch/normalize/upsert istisnası |

`unknown_symbol` domain tarafında **kullanılmaz**: anahtar sembol değildir.

### 8.2 404 kuralının bilinçli istisnası

Domain dataset'leri `call_optional` **kullanmaz**, `call_yahoo` kullanır.
**404 → `failed`.**

AH§8.4'ün "404 → `empty`" kuralı sembol tarafı içindir: orada anahtar
kullanıcının verdiği bir semboldür ve o sembolde o modülün olmaması
meşrudur (`^GSPC`'de `esgScores` yok). Burada anahtar **aynı koşuda kendi
keşfimizden** gelir; 404 "Yahoo bu endüstriyi kaldırdı ya da yeniden
adlandırdı" demektir ve denetimde görünmesi **gerekir**.

Kural gevşetilseydi: kütüphane sabitinden seed eden bir gelecek değişiklik
32 endüstriyi sessizce `empty` yazar, DB'de 145 yerine 113 endüstri olur ve
denetim "hata yok" derdi. Bu spec'in ilk bulgusu tam olarak bu tuzaktı
(§4.1).

### 8.3 `yfin domain audit` — üç bağımsız kontrol

```sql
-- 1) Sektor sayisi
SELECT COUNT(*) FROM domains WHERE domain_type = 'sector';           -- = 11

-- 2) Endustri sayisi: BEKLENEN DEGERI API'NIN KENDISI VERIYOR.
--    DIKKAT: `as_of_date = :as_of` KESIN ESITLIK KULLANILAMAZ -- kapi
--    hash'i esit bulursa o gun HIC domain_metrics satiri yazilmaz
--    (as_of_date VOLATILE, satir da yeniden yazilmaz) ve audit sahte
--    basarisizlik verirdi. Her sektorun :as_of'a KADARKI EN SON satiri
--    alinir.
WITH son AS (
  SELECT m.domain_key, m.industries_count,
         ROW_NUMBER() OVER (PARTITION BY m.domain_key
                            ORDER BY m.as_of_date DESC) AS rn
    FROM domain_metrics m
    JOIN domains d USING (domain_key)
   WHERE d.domain_type = 'sector' AND m.as_of_date <= :as_of
)
SELECT SUM(industries_count) FROM son WHERE rn = 1;                  -- = 145

SELECT COUNT(*) FROM domains WHERE domain_type = 'industry';         -- = 145

-- 3) Hucre kapsami ve hata
SELECT dataset, table_name, region, status, COUNT(*)
  FROM sync_run_items WHERE run_id = :run
 GROUP BY 1,2,3,4;                       -- failed = 0, beklenen hucre sayisi tam
```

2. kontrol bu tasarımın çekirdeğidir: beklenen değer **bizim listemizden
değil**, Yahoo'nun `overview.industriesCount` alanından gelir ve 11/11
sektörde liste uzunluğuna eşit ölçüldü (toplam 145). Yahoo yeni bir endüstri
eklerse ve keşfimiz onu kaçırırsa bu kontrol patlar.

Beklenen hücre sayısı (R = bölge sayısı):
```
domain_taxonomy   : 2 tablo × 1 tur                        =   2
sector_profile    : 4 tablo × 11 anahtar × 1 bolge         =  44
sector_rankings   : 3 tablo × 11 anahtar × R               =  33 × R
industry_profile  : 5 tablo × 145 anahtar × 1 bolge        = 725
industry_rankings : 3 tablo × 145 anahtar × R              = 435 × R
                                              R=1 -> 1 239 hucre
                                              R=5 -> 3 143 hucre
```

`industry_profile` **beş** tablo yazar (`domains` dâhil, §7.3);
`sector_profile` dört — sektör `description`/`message_board_id` alanlarını
zaten bootstrap dolduruyor, çünkü onlar sektör yanıtının `overview`
bloğunda var.

`domain audit` başarısızlıkta çıkış kodu **1** döndürür.

### 8.4 Eksiksizlik iddiasının kanıt zinciri

| İddia | Kanıt |
|---|---|
| Taksonomi eksiksiz | `SUM(industries_count)` == `COUNT(domains WHERE type='industry')` — beklenen değeri API veriyor |
| Hiçbir alan atılmadı | Her anahtar ya tipli kolona ya `raw_json`'a gider; `_MAPPED_*` dışındaki her anahtar `WARNING` üretir |
| Hiçbir hücre sessizce boş kalmadı | 404 → `failed`; her `(dataset × anahtar × bölge × tablo)` için bir `sync_run_items` satırı |
| Bölge etiketi doğru | Run başında ampirik prob; `XX`/`EUROPE` run'ı başlatmadan reddedilir |
| Yazılan = okunan | `apply_write`'ın anahtar-varlığı doğrulaması; uyuşmazlık hücreyi `FAILED` yapar |
| Anahtar kaynağı bozulmadı | `test_domain_key_source.py` kütüphane sabitinden türetilen kümenin farklı olduğunu sürer |

---

## 9. Test stratejisi

### 9.1 Fixture'lar — `tests/fixtures/_domain/`

Ham JSON zarfları (`{"data": {...}}`), `scripts/capture_fixtures.py`'a
eklenen `--domain` moduyla bir kez yakalanır. Her referans anahtar **tek
başına bir kenar durumun kanıtıdır**:

| Fixture | Kanıtladığı |
|---|---|
| `sector/technology.json` | Tam yanıt: 12 endüstri, 10 ETF + 10 fon, 4 rapor, `performance` + benchmark; `FFOMX` fon adı yok |
| `sector/utilities.json` | **6/6 endüstri anahtarı** kütüphane sabitinden farklı — anahtar kaynağı kuralının çekirdek kanıtı |
| `sector/healthcare.json` | Fon sembolü `0P0001WO1I` (Morningstar kimliği, ticker değil) → `is_known=0` |
| `sector/technology.GB.json` | Bölge kapsamı: `topETFs` **boş**, `topCompanies` tamamen farklı; `overview`/`performance` US ile birebir aynı |
| `industry/semiconductors.json` | İki mover listesi dolu, `sectorKey` bağı, `industriesCount` **yok** |
| `industry/infrastructure-operations.json` | **Üç** liste bloğunun hiçbiri yok (`companiesCount=1`) |
| `industry/biotechnology.json` | `ytdReturn = 9999.0` sentinel değil |
| `industry/pharmaceutical-retailers.json` | `BHIC` iki listede birden, `growthEstimate` yok, `name` yok |
| `industry/electronic-components.json` | `growthEstimate = 81.5` |
| `industry/financial-services` altı bir endüstri + `sector/financial-services.json` | `companiesCount = 1517` — üst sınır; `INTEGER` yeterliliğinin kanıtı |
| En uzun `reportTitle` taşıyan rapor (hangi fixture'da çıkarsa) | 23 570 karakter → `MEDIUMTEXT` round-trip'i |
| `industry/gold.json` | `topPerforming`'te `name` eksik |

### 9.2 `tests/unit/` — ağsız

| Dosya | Kapsam |
|---|---|
| `test_domain_parse.py` | `_unwrap`'in üç biçimi; `researchReports[].targetPrice`'ın **çıplak**, `topCompanies[].targetPrice`'ın **sarmalı** olduğu aynı testte sabitlenir |
| `test_domain_taxonomy.py` | "All Industries" `key` yokluğuyla elenir; adı değiştirilmiş bir fixture varyantı yine 12 satır üretir (**ada bakılmadığının kanıtı**) |
| `test_domain_asof.py` | Hash: satır sırası değişince **aynı**, `is_known` değişince **aynı**, Yahoo yeni alan ekleyince **farklı** |
| `test_domain_edge_cases.py` | §10 tablosunun her satırı |
| `test_domain_key_source.py` | **Regresyon çiti:** `SECTOR_INDUSTY_MAPPING_LC`'den türetilen endüstri kümesi ile fixture'lardan türetilen küme eşit **değildir** ve fark tam olarak em-dash/`&` anahtarlarıdır. Ayrıca `SECTOR_KEYS` üst düzey anahtarlarla eşittir |
| `test_domain_registry.py` | Alias çözümleme, bootstrap'in başa eklenmesi, `regional` bayrağının bölge döngüsünü belirlemesi |
| `test_domain_regions.py` | Biçim reddi (`EUROPE`, `''`); **taban her zaman US** (`YF_DOMAIN_REGIONS=XX` tek başına da reddedilir); prob sahte `fetch` ile: kesişim ≥ %50 → `ValueError`, kesişim 0 → geçer; **sırası/kümesi kaymış ama farklı** bir liste geçer (eşitlik kullanılmadığının kanıtı); `US` tek başınayken **hiç istek yapılmaz** |
| `test_asof_gate_unchanged.py` | `AsOfGate` ayrıştırmasından sonra mevcut 13 as-of dataset'inin `content_hash`'i ve kapı yazımı **bit-birebir aynı** (fixture'lardan üretilen hash'ler altın değer olarak sabitlenir) |
| `test_runner_region_optional.py` | `ItemRecord.region` eklendikten sonra sembol ve piyasa hücrelerinin `region`'ı `NULL` kalır |

### 9.3 `tests/repo/` — gerçek MySQL

| Dosya | Kapsam |
|---|---|
| `test_domain_schema.py` | `domain_key` `ascii_bin` (`'technology'` ve `'TECHNOLOGY'` iki ayrı satır olabilmeli); `CHECK` kısıtı `domain_type='industry'` + `parent_key=NULL`'ı reddeder; FK'ler ve `ON DELETE RESTRICT` |
| `test_domain_writes.py` | 10 fixture yazılır; satır sayıları ve tip round-trip'i (`9999.0`, `81.5`, `-9.999999999999998`, `2.88e13`, `1.32e-5` kayıpsız geri okunur) |
| `test_domain_idempotency.py` | Aynı fixture iki kez: ikinci koşuda **veri** tablolarının hücreleri `skipped`, satır sayısı değişmez. `domain_asof_state` hücresi `ok` kalır (kapı satırı her iki dalda da yazılır, `asof_base.py:140-145`) ve `domain_taxonomy` düz upsert olduğu için `symbols`/`domains` hücreleri de `ok` kalır — "tüm hücreler `skipped`" iddiası **yanlış** olurdu |
| `test_domain_audit.py` | Bir endüstri satırı elle silinince audit **1** döndürür |
| `test_domain_symbols.py` | `is_active` ezilmiyor: satır `is_active=1` yapılıp yeniden yazıldığında 1 kalıyor |
| `test_domain_scope.py` | `industry_rankings`, `domain_top_companies`'teki **sektör** satırlarını silmiyor (`scope_columns` `domain_key` içeriyor, §5.11) |
| `test_domain_prune.py` | Genelleştirilmiş `prune_asof` domain tablolarını son günü koruyarak buduyor; **sembol tarafındaki budama davranışı değişmiyor**; `--orphan-reports` yalnız bağsız raporları siliyor |
| `test_domain_is_known.py` | Bilinmeyen bir sembol `symbols`'a eklendiğinde ikinci koşuda kapı **açılıyor** ve `is_known` 1'e dönüyor (hash'te olmasının kanıtı) |
| `test_domain_reports.py` | Sektör ve endüstride ortak görünen raporun **tek** `domain_research_reports` satırı + **iki** `domain_report_links` satırı ürettiği (ölçüm: 37 tekil sektör raporunun hepsi bir endüstride de var); 23 570 karakterlik bir `report_title`'ın `MEDIUMTEXT`'te kayıpsız round-trip'i |

### 9.4 `tests/live/` — `-m live`, CI'da kapalı

`test_live_domain.py`: 11 sektör çekilir, `industriesCount` toplamı 145 mi;
rastgele 5 endüstri anahtarı 200 döndürüyor mu; `region='XX'` `ValueError`
veriyor mu; `SECTOR_KEYS`'in 11'i de 200 döndürüyor mu.

---

## 10. Kenar durumlar

| Durum | Ölçüm | Davranış |
|---|---|---|
| `infrastructure-operations` | `companiesCount=1`; **üç** liste bloğu da yok | `industry_rankings` → boş sonuç → kapı satırı **yazılmaz**, hücreler `empty`. `industry_profile` normal çalışır |
| GB/DE/JP/TR'de `topETFs` | 0 satır | `empty` |
| `semiconductors` TR'de | Hiçbir liste yok | `empty` |
| `rating` eksik | Sektörde **12/550**; `semiconductors`'ta **2/49** | NULL |
| Fon adı eksik | 220 fon satırının **7'sinde**, hepsi yatırım fonu tarafında | NULL |
| Fon sembolü ticker değil | `0P0001WO1I` | `is_known=0` |
| `ytd_return = 9999.0` | ELOX | **Sentinel sayılmaz**, olduğu gibi yazılır. `currentPriceTarget = 0.0`'ın NULL'a çevrilmemesiyle aynı ilke |
| Aynı sembol iki mover listesinde | Tam-liste ölçümü: **50 ortak, 8'inde farklı `ytdReturn`** (24 endüstri) | `rank_type` PK'da → iki satır da yazılır |
| ETF/fon sembol kesişimi | Bugün 0 | `fund_type` yine PK'da |
| Aynı rapor birden çok domain'de | Günde 624 satır → 516 tekil; 37 tekil sektör raporunun **hepsi** bir endüstride de var | Rapor upsert, bağ ayrı satır |
| `region` geçersiz | Sessizce US | US tabanına karşı **küme kesişimi** probu (§6.6), `ValueError`. `US` tek başınayken prob çalışmaz — gerek de yok |
| `region='us'` | `Domain.__init__` `.upper()` yapıyor | Config değerleri normalize + `^[A-Z]{2}$` |
| Anahtar 404 | `TECHNOLOGY`, `software—application` | `failed` |
| Boş anahtar | `KeyError('data')` | `DATA` → `failed` |
| `sectorKey` ≠ `domains.parent_key` | Bugün 145/145 tutarlı | `WARNING`; taksonomi kayması sinyali |
| `reportTitle` çok uzun | Max **23 570** karakter (104 rapor) | `MEDIUMTEXT`; `TEXT` utf8mb4'te taşabilirdi |
| Rapor alanı hiç yok | 104 raporun 17'sinde `targetPrice` | `row.get()` → NULL |
| Endüstri `overview`'unda `industriesCount` anahtarı yok | 145/145 | `row.get()` → NULL; `_MAPPED_OVERVIEW_KEYS` iki varyant |

---

## 11. Migration ve budama

### 11.1 Migration

Tek revizyon: `20260904_1730_sector_industry_domain.py`,
`down_revision = "e7f2a94c1b83"` (bugünkü head,
`migrations/versions/20260904_1700_history_metadata_tz_name.py`).

**upgrade:**
1. `create_table` sırası: `domains` → `domain_metrics` → `domain_top_companies`
   → `domain_top_funds` → `domain_top_movers` → `domain_research_reports`
   → `domain_report_links` → `domain_asof_state`
2. `ALTER TABLE sync_runs MODIFY COLUMN scope
   ENUM('symbols','market','domain') NOT NULL DEFAULT 'symbols'`
   — yeni değer **sona** eklendiği için `ALGORITHM=INSTANT`
3. `ALTER TABLE sync_run_items ADD COLUMN region VARCHAR(16)
   CHARACTER SET ascii COLLATE ascii_bin NULL` — `ALGORITHM=INSTANT`
4. `symbols`'a şema değişikliği **yok**

**downgrade:**
1. `UPDATE sync_runs SET scope = 'symbols' WHERE scope = 'domain'` —
   **ENUM daraltmasından ÖNCE**. Kurulu desen bunu zorunlu kılıyor
   (`20260904_1640_item_status_out_of_scope.py:36-40`, `out_of_scope` →
   `skipped`); yapılmazsa MySQL o satırların değerini sessizce `''` yapar
2. `sync_runs.scope` ENUM'u iki değere daraltılır
3. `sync_run_items.region` kolonu düşürülür *(bir kolon + bir ENUM
   değişikliği; ilk taslaktaki "iki kolon" ifadesi yanlıştı)*
4. 8 tablo düşürülür (FK ters sırası)

`symbols`'taki 156 satır **silinmez**: `ON DELETE RESTRICT` taşıyan tablolar
onlara bağlı olabilir ve silme `yfin symbols purge --force` kullanıcısının
kararıdır.

**Yeni desen uyarısı:** `domains`'in `CHECK` kısıtı kod tabanındaki **ilk**
`CheckConstraint`'tir (MySQL 8.0.16+ destekliyor, 8.3.0'da doğrulandı).

### 11.2 Budama — `prune.py` genelleştirilmesi

Bu **basit bir tablo listesi ekleme değildir**. `prune.py` bugün üç yerde
sembol tarafına sabitlenmiş:

| Yer | Sabit varsayım | Domain'de neden tutmaz |
|---|---|---|
| `prune.py:86-105` `asof_table_datasets()` | Yalnız `SYMBOL_DATASETS` üzerinde döner, `AsOfDataset` örneği arar | Domain dataset'leri `DOMAIN_DATASETS`'te ve `DomainAsOfDataset` |
| `prune.py:129-137` `_asof_protected()` | Kapı olarak `GATE_TABLE` (`asof_state`), gruplama kolonu `table.c["symbol"]` | Kapı `domain_asof_state`; kapsam kolonu `domain_key`. `domain_top_*`'ta `symbol` **vardır ama şirketin sembolüdür** — onunla gruplamak **yanlış koruma kapsamı** üretir |
| `prune.py:150-158` `prune_asof()` | `tuple_(table.c["symbol"], table.c["as_of_date"])` | Aynı |

**Değişiklik:** üçü de bir `(registry, gate_table, scope_column)` üçlüsüyle
parametrelenir; mevcut çağrılar varsayılanları
(`SYMBOL_DATASETS`, `"asof_state"`, `"symbol"`) kullanır ve **davranışları
değişmez**. `prune_asof` iki kez çağrılır — ikincisi
`(DOMAIN_DATASETS, "domain_asof_state", "domain_key")` ile.

`domain_asof_state`'in PK'sında `region` de var; koruma çiftleri
`(domain_key, as_of_date)`'e indirgenir — bölgeler arasında **daha tutucu**
koruma demektir, veri kaybı yönünde değil.

**Kapsam:** `domain_metrics`, `domain_top_companies`, `domain_top_funds`,
`domain_top_movers`, `domain_report_links` budanır (son gün korunur).
`domains` ve `domain_asof_state` budanmaz (kapı satırı silinseydi
`first_seen_at` kaybolur ve bütün geçmiş yeniden yazılırdı —
`prune.py:79-82`'nin kendi gerekçesi).

**Öksüz rapor:** `domain_research_reports` de budanmaz, ama
`domain_report_links` budandıkça hiçbir bağı kalmayan raporlar birikir. FK
`ON DELETE CASCADE` **ters yönde** çalışır (rapor silinince bağ silinir),
bu yüzden `news`'teki `prune_orphan_news` muadili bir adım gerekir:

```sql
DELETE r FROM domain_research_reports r
 LEFT JOIN domain_report_links l ON l.report_id = r.report_id
 WHERE l.report_id IS NULL;
```

`yfin prune --orphan-reports/--no-orphan-reports` bayrağıyla,
`--orphan-news` ile aynı desende. `YF_PRUNE_ENABLED` varsayılan kapalı
politikası aynen geçerlidir.

---

## 12. Yapılandırma

```python
yf_domain_regions: str = "US"                     # ISO 3166-1 alpha-2, virgullu
yf_domain_reference_sector: str = "technology"    # bolge dogrulama probu
```

**İki ayar, üç değil.** İlk taslakta bir `yf_domain_include_reports` bayrağı
vardı; çıkarıldı, çünkü hiçbir yerde okunmuyordu ve okunsaydı profil
dataset'inin `produces`'ını koşullu yapar, §8.3'ün hücre sayısını
yapılandırmaya bağımlı kılardı. Rapor toplamak istemeyen kullanıcı zaten
`--datasets sector_rankings,industry_rankings` diyebilir.

`yf_domain_regions` birden çok bölge içerdiğinde ilki **birincil**dir:
bölgesiz dataset'ler onun yanıtını kullanır ve doğrulama probu onu taban
alır.

---

## 13. CLI

```
yfin domain sync    [--datasets all|sector|industry|<ad,ad>]
                    [--regions US,GB]        # YF_DOMAIN_REGIONS'i ezer
yfin domain audit   [--as-of YYYY-MM-DD]
yfin domain datasets
yfin domain list    [--type sector|industry] [--parent <sector_key>]

yfin prune          [--orphan-reports/--no-orphan-reports]   # YENI (S11.2)
```

`domain sync` kendi advisory lock'unu (`yfin_domain_sync`) alır. Çıkış
kodları mevcut sözleşme: 0 = ok, 1 = kısmi/başarısız,
`EXIT_LOCK_NOT_ACQUIRED` = kilit alınamadı.

`--regions` ezmesi `Settings`'in **kopyası** üzerinden yapılır —
`domain_regions(settings)` yalnız `settings.yf_domain_regions` okur, bu
yüzden CLI değeri `settings.model_copy(update={"yf_domain_regions": value})`
ile enjekte edilir ve doğrulama (biçim + ampirik prob) CLI'dan gelen değere
de **aynen** uygulanır. `--shards`'ın `YF_MAX_SHARDS`'ı ezmesiyle aynı
desen; fark, oradaki değerin doğrulanacak bir şeyi olmaması.

`yfin datasets` çıktısına üçüncü satır eklenir:
`domain : industry, sector, industry_profile, …`

---

## 14. Uygulama sırası

**0. Kırılganlık çiti önce:** `test_domain_key_source.py` (§9.2) — bu spec'in
varlık nedeni odur ve hiçbir uygulama kodundan önce yazılır.

1. **`AsOfGate` mixin'inin ayrıştırılması** (§6.2) + `VOLATILE_COLUMNS`'a
   `first_seen_at` + `asof_produces(gate=…)`. **Mevcut 13 as-of dataset'inin
   testleri bu adımdan sonra da aynen geçmelidir** — davranış değişikliği
   sıfır olmalı. Bu adım domain kodundan önce ve ondan **bağımsız** olarak
   birleştirilebilir
2. `ItemRecord.region` + `_record_items` / `_failed_records` /
   `_skipped_records` / `write_items` imza genişletmesi (§8.1) — yine sıfır
   davranış değişikliği
3. `prune_asof` ailesinin `(registry, gate_table, scope_column)` ile
   parametrelenmesi (§11.2) — yine sıfır davranış değişikliği
4. Modeller (`models/domains.py`) + `RunScope.DOMAIN` + migration
   (§11.1) — 8 tablo, 1 kolon, 1 ENUM
5. `DomainContext` / `DomainDataset` / `DomainAsOfDataset` / `DOMAIN_DATASETS`
6. `datasets/domain/common.py` — `_unwrap`, `fetch_domain`, `SECTOR_KEYS`,
   `_MAPPED_*` kümeleri (`overview` için **iki** varyant, §7.6)
7. `domain_taxonomy` + `symbols` yazımı
8. `sector_profile` / `industry_profile` (ikincisi `domains`'e de yazar)
9. `sector_rankings` / `industry_rankings`
10. `domain_runner` + `domain_regions()` bölge doğrulaması + proxy sırası
11. CLI (`sync`, `audit`, `datasets`, `list`) + config + `--orphan-reports`
12. Fixture yakalama (`--domain`) + §9'un kalan testleri

1–3. adımlar **mevcut kodu genelleştirir ve hiçbir davranışı değiştirmez**;
ayrı bir PR olarak birleştirilebilir ve domain işi onların üstüne oturur.
Her adım TDD ile: önce test, sonra uygulama.
