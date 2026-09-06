# yfinance Analysis & Holdings → MySQL — Tasarım Dokümanı

- **Tarih:** 2026-09-04
- **Durum:** Üç bağımsız incelemeden geçti, revize edildi
- **Proje kökü:** `~/Projects/learn/yfinance/`
- **Temel doküman:** `2026-09-04-yfinance-mysql-etl-design.md` (bundan sonra **T**, ör. T§8.3)
- **Önceki genişletmeler:** `2026-09-04-yfinance-financials-market-design.md` (**F**),
  `2026-09-04-proxy-pool-and-yfinance-advanced-design.md` (**P**)
- **Kapsam:** `https://ranaroussi.github.io/yfinance/reference/yfinance.analysis.html`
  sayfasındaki "Analysis" + "Holdings" bölümlerinin tamamı

---

## 0. Önkoşullar

Bu doküman **T**, **F** ve **P**'yi genişletir, hiçbirinin yerine geçmez.
Uygulama aşağıdakilerin kodda bulunmasını varsayar; hepsi 2026-09-04'te
doğrulandı:

| Önkoşul | Nerede |
|---|---|
| `Registry` sınıfı, `SYMBOL_DATASETS` / `MARKET_DATASETS` | `datasets/registry.py` |
| `TableWrite.scope_columns`, **`scope_values`**, `monotonic_columns` | `datasets/base.py:22-40` |
| `RowWriter.current_hash(table, key: Mapping)` — anahtar **zaten genelleştirilmiş** | `persistence.py:37` |
| `HashGatedDataset` — hash kapısı + çocuk tablo atlama | `datasets/hash_gated.py` |
| `SnapshotDataset` | `datasets/snapshot_base.py` |
| `errors.classify_error()` / `ErrorKind` / `_kind_from_status()` | `errors.py:107-183` |
| **`errors.is_absent_data()` + `client.call_optional()`** — 404 → `empty` | `errors.py:197-209`, `client.py:74-86` |
| `client.py` zaten `hide_exceptions=False`, `retries=0` yapıyor | `client.py:149-153` |
| `shard.py` / `ShardSpec` — çok process'li çalıştırma | `shard.py:56-73` |
| `market_runner.py`, `sync_runs.scope` (`RunScope`) | `market_runner.py`, `models/sync.py` |
| `FactValueType()`, `KeyTextType()`, `AsciiKeyType()`, `ShortHashType()` | `models/base.py` |

Bu tipler ve sözleşmeler **yeniden tanımlanmaz**. Bu spec dört ekleme yapar:
`AsOfDataset` (§6.1), `Dataset.date_range` (§6.2), `SyncContext.start/end`
(§6.2) ve 17 dataset'in `fetch`'inde `client.call_optional` kullanımı (§8.4).
(`ItemStatus` TÜRETMESİ değişmez — ayrım uçta, açık çağrıyla yapılır.) **`RowWriter`
protokolüne dokunulmaz** — mevcut `current_hash` yeterlidir.

---

## 1. Amaç

Kurulu veri hattına yfinance'ın **analist**, **sahiplik/insider** ve **fon
içerik** verilerini eklemek. Mevcut ilkeler değişmez: eksiksizlik, tip
güvenliği, idempotency, sembol kodu üzerinden ilişki, makine tarafından
doğrulanabilir "eksiksiz yazıldı" iddiası.

Bu genişletme mevcut mimarinin üç varsayımını zorluyor; üçü de açıkça
çözülüyor:

1. **Her veri kaynağa ait bir tarih taşır.** Analist verilerinin çoğu yalnızca
   "şu an"ı döndürür ve dönem etiketi **görelidir** (`0q`, `+1y`, `0m`, `-1m`) —
   yani zaman damgası olmadan veri **anlamsızdır** (§6.1).
2. **Her dataset kendi HTTP isteğine sahiptir.** Burada 17 dataset yedi isteği
   paylaşıyor; `earningsTrend` tek başına dört dataset'i, holders demeti altı
   dataset'i besliyor (§4.4).
3. **Boş sonuç ile hata birbirinden ayırt edilebilir.** Ayırt edilemiyor:
   yfinance varsayılan ayarıyla ağ hatasını **sessizce boş sonuca** çeviriyor
   (§8.4). Ayarı kapatınca bu kez Yahoo'nun "bu sembolde bu modül yok" (404)
   yanıtı `failed` görünüyor. İkisini ayıran tek şey HTTP durum kodudur.

### Kapsam içi

**17 sembol-kapsamlı dataset:**
`recommendations`, `upgrades_downgrades`, `analyst_price_targets`,
`earnings_estimate`, `revenue_estimate`, `eps_trend`, `eps_revisions`,
`earnings_history`, `growth_estimates`, `sustainability`, `major_holders`,
`institutional_holders`, `mutualfund_holders`, `insider_purchases`,
`insider_transactions`, `insider_roster_holders`, `funds_data`.

**4 alias:** `recommendations_summary` → `recommendations`; `analysis`,
`holders`, `funds` grup alias'ları (§6.3).

**18 yeni tablo**, `sync_runs`'a **1 kolon**, sözleşmeye **4 ekleme**,
`runner.py`'de **6 nokta**, `shard.py`'de **2 nokta**, `cli.py`'ye
**5 seçenek + 1 alt komut** (§6.5).

Kapsama ayrıca iki hat-geneli yetenek girer, çünkü ikisi de bu dataset
ailesinin doğruluk sorunlarını doğrudan çözer:

- **Borsa/tip/sonek ile sembol seçimi** (§6.4) — filtrelenmeyen egzotik
  semboller §8.4'ün 404 kuralıyla birlikte bile sembol başına yedi boşa
  istek üretir.
- **`--start` / `--end` aralığı** (§6.2, §7.3) — üç dataset'te satır eleme,
  mevcut beş dataset'te gerçek geriye dönük çekim.

### Kapsam dışı — gerekçeli, tahminle değil ölçümle

| Çıkarılan | Gerekçe (ölçüm) |
|---|---|
| `sustainability` **tablosu** | `esgScores` modülü **19 sembolde de** `404` verdi (AAPL, MSFT, KO, XOM, TSLA, JPM, NVDA, GE, PFE, WMT, BA, INTC, DIS, F, THYAO.IS, NESN.SW, BP.L, 005930.KS, BABA — tech, enerji, ilaç, finans, perakende, otomotiv, savunma, medya; US/TR/CH/UK/KR/CN). Hiç dolmayacak bir tablo açmak yerine **izleme dataset'i** (§6.3) |
| `recommendations_summary` **tablosu** | Kaynakta `return self.get_recommendations(as_dict=as_dict)` (`base.py:220-221`) — birebir aynı fonksiyon; **alias** |
| `get_valuation_measures` / `valuation` | Bu doküman sayfasında **yok**; `Financials` bölümünün parçası. **Kapandı:** F spec'i **Ek B** — ayrı tablo açmadan `financial_periods`/`financial_facts` EAV'sine `statement='valuation'` olarak girer |
| Analist tahminlerinin **geçmişe dönük çekimi** | Yahoo'da böyle bir uç **yoktur**. 17 metodun imzası `as_dict` dışında parametre almıyor (`base.py:210-372`). `as_of_date` geçmişi ilk çalıştırmadan **ileriye** birikir (§7.3) |
| `majorDirectHolders` | yfinance ayrıştırmayı kendi kaynağında kapatmış (`holders.py:92`); erişilebilir property yok |

---

## 2. Kararlar ve gerekçeleri

| Karar | Seçim | Gerekçe |
|---|---|---|
| As-of geçmişi | Tek tablo, PK'da `as_of_date` + merkezî `asof_state` kapı tablosu | Bu dataset'ler sembol başına çok satırlıdır (4–10); `snapshot + _history` çifti 26+ tablo ve iki ayrı PK şeması üretirdi |
| As-of kapısının yeri | `asof_state`, mevcut `RowWriter.current_hash(table, key)` ile okunur | Anahtar zaten `Mapping` alıyor; **protokole yeni metot gerekmez** |
| Kapı satırı | **Her durumda yazılır**; hash eşitse yalnız `fetched_at` | `hash_gated.py:11-13`'ün kurduğu ilke: `fetched_at` "son **doğrulama** zamanı"dır. Sapmak "bu sembol en son ne zaman kontrol edildi" sorusunu cevapsız bırakırdı |
| Hash girdisi | Satırlar `key_columns`'a göre **sıralanır**, `as_of_date`/`fetched_at` **dışlanır** | `canonical_json` yalnız sözlük anahtarlarını sıralar (`normalize.py:317`); liste sırası korunur. Kaynak sırayı değiştirince içerik aynıyken hash değişirdi |
| Ağ hatası vs veri yokluğu | `hide_exceptions=False` **+ HTTP 404 → `empty`** | Ayar tek başına her egzotik sembolde 16 `failed` üretir (§8.4); 404 kuralı olmadan kullanılamaz |
| `earnings_estimate` + `revenue_estimate` | Tek `analyst_estimates`, `metric` ENUM'u PK'da | Kolon setleri birebir aynı; tek modülden geliyorlar |
| `institutional_holders` + `mutualfund_holders` | Tek tablo, `holder_type` ENUM'u PK'da | Kolon setleri 14 sembolde birebir aynı ölçüldü |
| `fund_metrics` PK | `section` **PK'ya dâhil** | Aynı `metric` adı iki section'da gelirse `ERROR 1062`; kardeş `fund_weightings` zaten `category`'yi PK'ya koyuyor |
| Estimate değer tipi | `FactValueType()` = `DECIMAL(38,10)` | Aynı kolonda EPS `1.97656` ve revenue `1 285 436 390 920` |
| `insider_transactions` anahtarı | **Birebir tekilleştirme** + `fact_hash` | PFE'de dokuz kolonun tamamında özdeş iki satır var; `fact_hash` onları ayıramaz. F§8.3'ün `earnings_dates` kuralının aynısı |
| `insider_purchases` şekli | 7 satırlık sunum tablosu tek satıra pivotlanır | Kaynak zaten tek kaydın 7 satırlık gösterimi; 0. kolon adı dinamik |
| `funds_data` şeması | Hibrit: 3 tipli tablo + 1 EAV | Hisse fonunda 11 sektör + 1 rating, tahvil fonunda **0 sektör + 9 rating** |
| `funds_data` ön kontrolü | `fast_info['quoteType']`, yoksa `history_metadata['instrumentType']` | 12 sembolde ikisi de birebir aynı ve **aynı chart isteğinden** besleniyor; ek istek yok |
| Aralık desteği | `date_range: "api" \| "filter" \| "none"` | Üç dataset'te satır eleme anlamlı, 14'ünde anlamsız; `"api"` mevcut beş dataset için §10/12'de bağlanır |
| Sembol seçimi | `--exchange` / `--quote-type` / `--suffix` | Egzotik semboller filtrelenmezse yedi boşa istek |
| `action`, `price_target_action` | `VARCHAR`, ENUM değil | 5 ve 7 değer ölçüldü; kapalı liste olduğunun kanıtı yok |
| Ticker paylaşımı | Sembol başına **tek** `Ticker` | 17 çağrıyı 7 isteğe indiren tek şey bu |

---

## 3. Ortam

**T**§3 ile aynı: MySQL 8.3.0 (Docker, host `localhost`, şema `yfinance`,
test şeması `yfinance_test`), Python 3.13.5, yfinance 1.7.0, pandas 3.0.5,
numpy 2.5.2.

`SELECT VERSION()` → `8.3.0`, `@@innodb_default_row_format = dynamic`,
InnoDB PK sınırı **3 072 byte** (sınır değerle doğrulandı: `VARCHAR(768)`
utf8mb4 PK geçer, `VARCHAR(769)` → `ERROR 1071`).

Tüm §4 bulguları 2026-09-04'te canlı Yahoo çağrılarıyla üretildi ve
**bağımsız bir ikinci ölçümle** (29 hisse + 10 fon + 4 egzotik sembol)
sınandı; §4.2 çürütülenleri içerir.

---

## 4. API keşif bulguları

### 4.1 Doğrulanmış davranışlar

| Bulgu | Gözlem | Tasarıma etkisi |
|---|---|---|
| İstek paylaşımı | 17 çağrı, tek `Ticker` örneğinde **8 modül isteği** (`sustainability` dâhil) | §4.4 |
| `Analysis`/`Holders`/`Quote` ömrü | `Ticker.__init__`'te bir kez kurulan örnek nitelikleri (`base.py:116-118`) | Paylaşım mevcut tek-Ticker bağlamıyla kendiliğinden oluşur |
| `recommendations` | Kolonlar `period, strongBuy, buy, hold, sell, strongSell`; `period` ∈ `0m,-1m,-2m,-3m`; `period` dtype `str`, sayaçlar `int64`, NaN yok | Satır sayısı **değişken**: 19 sembolün 10'unda 4, **9'unda 3** |
| `upgrades_downgrades` kolonları | **7 kolon** (dokümanda 4): +`priceTargetAction`, `currentPriceTarget`, `priorPriceTarget`. 15 sembolde aynı | Üç ek tipli kolon |
| `upgrades_downgrades` anahtarı | 15 sembolde **8 852 satır**, `(GradeDate, Firm)` **dup=0**; `GradeDate` tek başına bile tekil | PK `(symbol, grade_ts_utc, firm)` |
| `upgrades_downgrades` index tipi | `datetime64[s]`, tz-naive; `epochGradeDate` saniyesinden `pd.to_datetime(unit='s')` | Değer UTC'dir |
| `Action` / `priceTargetAction` | `down,init,main,reit,up` / `'',Adjusts,Announces,Lowers,Maintains,Raises,Removes` | `VARCHAR`, `''` → NULL |
| `currentPriceTarget` alt sınırı | `0.0` gerçek değer olarak geliyor | **NULL'a çevrilmez** |
| Estimate index'i | `0q, +1q, 0y, +1y` — 19/19 sembolde | `period` PK'da |
| Estimate `currency` kolonu | **Dokümanda yok**; `eps_revisions`'ta da var | Ayrı kolon |
| **`eps_revisions` kolon adı** | `upLast7days, upLast30days, downLast30days, **downLast7Days**` — 19/19 sembolde | Son anahtarda **D büyük** |
| Estimate büyüklük aralığı | EPS `1.97656` ile revenue `1 285 436 390 920` aynı şemada | `DECIMAL(38,10)` |
| `numberOfAnalysts` | `float` (`1.0`) ve `NaN` gelebiliyor | `INT UNSIGNED NULL` |
| `revenue_estimate.avg = 0` | THYAO `0q`/`+1q`'da `0`, `NaN` değil | **Sıfır ≠ NULL** |
| `growth_estimates` | Index `0q,+1q,0y,+1y,**LTG**`; kolonlar yalnız `stockTrend, indexTrend` — 19/19 | `industryTrend`/`sectorTrend` hiç gelmiyor |
| `indexTrend` | 19/19 sembolde **birebir aynı** (`[0.496, 0.2409, 0.3159, 0.1522, 0.122]`) | Denormalize saklanır (§5.1) |
| `earnings_history` | 17 sembolde `(4,4)`; index `quarter` tz-naive `Timestamp`. Mali takvim sembole göre kayar (NVDA/WMT `2025-10-31…2026-07-31`) | `quarter_end` DATE, tz dönüşümü yok |
| `major_holders` | 19/19'da aynı 4 anahtar, tek kolon `Value`; `institutionsCount` float (`7750.0`) | Tipli 4 kolonluk tek satır |
| Holder tabloları | 14 sembolde kolon setleri **birebir aynı** | Tek tablo + `holder_type` |
| `Date Reported` | **Satır bazında değişir** — AAPL mutualfund'da tek listede 4 farklı tarih | Tablo başlığına taşınamaz |
| Holder büyüklükleri | `Value` max **1,76×10¹³** (JPM), `Shares` max **1,94×10⁹**; `Holder` max **70** | `DECIMAL(38,0)`, `VARCHAR(128)` |
| `insider_purchases` | 19/19'da `(7,3)`; satır etiketleri sabit; 0. kolon adı `Insider Purchases Last 6m` | Etiket **konumdan** okunur |
| `insider_purchases` negatif değer | KO net `-547 806` | İşaretli `DECIMAL(38,0)` |
| **`Ownership` değerleri** | `D`, `I` **ve `D/I`** (XOM) — 3 karakter | `AsciiKeyType(8)` |
| `insider_transactions` boş kolonlar | `Transaction` ve `URL` — 16 sembol, **1 464 satırın hepsinde** `''` | Sentinel → NULL |
| `insider_transactions.Value` | DIS ve BP.L'de **tüm satırlarda** NaN | NULL |
| **`insider_roster` kolon seti** | Sembole göre **7 / 9 / 11**; NVDA'da +`positionSummary`, +`positionSummaryDate`. Kolon **sırası** da sabit değil | `row.get(...)`, iki ek kolon (§5.2) |
| `Position Indirect Date` | `float64` (ham epoch) gelebiliyor; **6 sembolde dolu değer** ölçüldü (KO, TSLA, NVDA, WMT, JPM, INTC) | `dt` kind'ı iki biçimi de kabul eder |
| String uzunlukları | `position` max **56** (WMT), `insider` max **33** (BP.L, `'Elliott Investment Management L.P'` — kurumsal ad), `text` max 79, `most_recent_transaction` 45 | §5.2 tipleri |
| dtype kararsızlığı | `insider_purchases.Shares` ∈ {`Float64`,`Int64`}; `insider_transactions.Value` ∈ {`float64`,`int64`}; `Shares Owned Directly` ∈ {`float64`,`int64`} | Fixture testleri tek dtype'a bağlanmaz |
| `funds_data` fon olmayanda | `hide_exceptions=True` → `YFDataException`; **`False` → ham `KeyError('topHoldings')`** | İkisi de yakalanır (§6.3) |
| `funds_data.description` | Fon olmayanda **ikinci erişimde** şirket özetini döndürüyor (AAPL 1 825 karakter); `quote_type()` `'EQUITY'` | Kısmen dolu nesneye güvenilmez |
| `fund_operations`/`equity_holdings`/`bond_holdings` | Kolonlar `[<SEMBOLÜN KENDİSİ>, 'Category Average']` | 0. kolon **konumdan** |
| `asset_classes` | 10 fonun **10'unda da** aynı 6 anahtar | Tipli kolon |
| `sector_weightings` / `bond_ratings` | Hisse fonunda 11 + 1; tahvil fonunda **0 + 9** (BND, TLT, AGG) | EAV zorunlu |
| `top_holdings` | `holding_symbol` max 9 (`005930.KQ`), `holding_name` max 51; 10 fonda `dup=0`. Holding'ler arasında **fon sembolü** (`VRTPX`, `BISXX`) ve yabancı borsa (`2330.TW`, `0700.HK`) var | FK'sız `holding_symbol` + `is_known` |
| Egzotik semboller (`hide_exceptions=True`) | `^GSPC`, `EURUSD=X`, `GC=F`, uydurma → 17 çağrının hepsi boş `DataFrame` | §8.4 ile birlikte okunmalı |

### 4.2 Çürütülen varsayımlar

Aşağıdakiler ilk taslakta ya da resmî dokümantasyonda yanlıştı. Son beşi
**bu spec'in ilk sürümünde** yanlıştı ve bağımsız incelemede çürütüldü.

| Yanlış varsayım | Gerçek | Kanıt |
|---|---|---|
| `sustainability` bazı sembollerde dolu | **19 sembolün 19'unda da boş**, uç 404 | 8 sektör, 6 ülke |
| `recommendations_summary` ayrı veri kümesi | `get_recommendations`'ın kendisi | `base.py:220-221` |
| `upgrades_downgrades` 4 kolonlu (doküman) | **7 kolon** | 15 sembol |
| `growth_estimates` index'i `+5y/-5y` (doküman) | `0q,+1q,0y,+1y,**LTG**` | 19/19 |
| `growth_estimates` kolonları `stock,industry,sector,index` (doküman) | Yalnız `stockTrend, indexTrend` | 19/19 |
| `indexTrend` sembole özgü | Tüm sembollerde aynı | 19/19 |
| `eps_revisions` anahtarları hep küçük harfle biter | `downLast7Days`'te **D büyük** | 19/19 |
| Estimate'lerde `currency` yok (doküman) | Dört estimate çerçevesinde de var | AAPL `USD`, THYAO `TRY` |
| `insider_roster_holders` sabit kolon setli | **7 / 9 / 11**, sıra da değişken | NVDA 11 kolon |
| `funds_data` fon olmayanda boş döner | İstisna fırlatır | AAPL, BTC-USD, THYAO.IS |
| Bu uçlarda tarih aralığı verilebilir | **Hiçbirinin parametresi yok** | `base.py:210-372` |
| **`fast_info['quoteType']` SPY'da `None`** | **`'ETF'`** — ilk ölçüm anahtarı `quote_type` (snake_case) diye okumuş; doğrusu `quoteType`. 12 sembolde `fast_info['quoteType']` ile `history_metadata['instrumentType']` **birebir aynı** ve **aynı chart isteğinden** besleniyor | `fi.get('quote_type')` → `None`; `fi.get('quoteType')` → `'ETF'` |
| **`fact_hash` `insider_transactions`'ı tekilleştirir** | **Etmiyor:** PFE'de **dokuz kolonun tamamında** özdeş iki satır var (`BOSHOFF CHRISTOFFEL`, 8741 hisse, 263716 değer, 2025-02-21) → hash de özdeş | 16 sembol / 1 464 satır; MSFT 1, PFE 1, DIS 1, BP.L 2 çakışma; PFE'ninki hash'le **ayrışmıyor** |
| **`Ownership` yalnız `D`/`I`** | **`D/I`** de var (XOM) — `VARCHAR(2)` kırpar | 24 sembol |
| **`insider_transactions` penceresi ~24 ay** | Sınır **zaman değil, 150 satır**: 24 sembolün 12'sinde tam 150 satır; WMT ve BP.L'de pencere **12 aya**, META'da ~10 aya iniyor | `WMT n=150 2025-08-21→2026-09-01` |
| **`upgrades_downgrades` bugüne kadar veri verir** | **META'nın son kaydı 2024-09-30** (413 satır) | `date_range="filter"` "kaynak güncel" varsayamaz |
| **`hide_exceptions=False` yalnız ağ hatalarını açar** | Yahoo'nun "bu modül bu sembolde yok" 404'lerini de açar: egzotik sembolde **16 dataset**, THYAO.IS'te `upgrades_downgrades`, **her sembolde** `sustainability` istisna fırlatır | §8.4 — 404 kuralı zorunlu |

Ölçüm düzeltmeleri (tasarımı değiştirmeyen): `position` max 23 → **56**;
`insider` max 26 → **33**; `Holder` max 69 → **70**; holder `Value` max
`3,82×10¹¹` → **`1,76×10¹³`**. Dört tip de zaten yeterliydi.

### 4.3 Çok pazarlılık matrisi

17 dataset × 6 sembol, `hide_exceptions=True` altında. **Hiçbiri exception
fırlatmadı** (aynı çağrılar `hide_exceptions=False` ile 404 fırlatır — §8.4).

| dataset | AAPL | MSFT | THYAO.IS | SPY | VFIAX | BTC-USD |
|---|---|---|---|---|---|---|
| recommendations | 4×6 | 4×6 | **3**×6 | 0×0 | 0×0 | 0×0 |
| upgrades_downgrades | 972×7 | 930×7 | **0×0** | 0×0 | 0×0 | 0×0 |
| analyst_price_targets | 5 anahtar | 5 | 5 | **0** | **0** | **0** |
| earnings_estimate / revenue_estimate | 4×7 | 4×7 | 4×7 | 0×0 | 0×0 | 0×0 |
| eps_trend | 4×6 | 4×6 | 4×6 | 0×0 | 0×0 | 0×0 |
| eps_revisions | 4×5 | 4×5 | 4×5 | 0×0 | 0×0 | 0×0 |
| earnings_history | 4×4 | 4×4 | **0×0** | 0×0 | 0×0 | 0×0 |
| growth_estimates | 5×2 | 5×2 | 5×2 | 0×0 | 0×0 | 0×0 |
| sustainability | **0×0** | **0×0** | **0×0** | 0×0 | 0×0 | 0×0 |
| major_holders | 4×1 | 4×1 | 4×1 | 0×0 | 0×0 | 0×0 |
| institutional / mutualfund_holders | 10×6 | 10×6 | **0×0** | 0×0 | 0×0 | 0×0 |
| insider_purchases | 7×3 | 7×3 | 7×3 | 0×0 | 0×0 | 0×0 |
| insider_transactions | 77×9 | 99×9 | **0×0** | 0×0 | 0×0 | 0×0 |
| insider_roster_holders | 10×7 | 10×7 | **0×0** | 0×0 | 0×0 | 0×0 |
| funds_data | EXC | EXC | EXC | **dolu** | **dolu** | EXC |

**Fon matrisi** (10 fon):

| fon | tip | top_holdings | sectors | bond_ratings |
|---|---|---|---|---|
| SPY, QQQ, ARKK, VNQ, EEM | ETF | 10 | 11 | 1 |
| VFIAX / FCNTX | MUTUALFUND | 10 / 8 | 11 | 1 |
| **BND, TLT** | ETF | **0** | **0** | **9** |
| **AGG** | ETF | **1** | **0** | **9** |

### 4.4 İstek maliyeti — ölçüldü ve bağımsız doğrulandı

Tek `Ticker`, `YfData.get_raw_json` sarmalanarak sayıldı:

| Yahoo modülü | beslediği dataset | istek |
|---|---|---|
| `recommendationTrend` | `recommendations` (+ alias) | 1 |
| `upgradeDowngradeHistory` | `upgrades_downgrades` | 1 |
| `financialData` | `analyst_price_targets` | 1 |
| `earningsTrend` | `earnings_estimate`, `revenue_estimate`, `eps_trend`, `eps_revisions` | 1 |
| `earningsHistory` | `earnings_history` | 1 |
| `industryTrend,sectorTrend,indexTrend` | `growth_estimates` (ayrıca `earningsTrend`'i de okur) | 1 |
| holders demeti (7 modül) | 6 sahiplik dataset'i | 1 |
| `esgScores` | `sustainability` | 1 |
| `quoteType,summaryProfile,topHoldings,fundProfile` | `funds_data` | 1 (yalnız fonlarda) |

**Sembol başına net ek yük: 7 istek** (`sustainability` varsayılan kapalı,
`funds_data` yalnız fonlarda → fonda 8). Karşılaştırma için mevcut hattın
maliyeti: `history` 1, `info` 3.

Bu paylaşımın **tek koşulu** sembol başına tek `Ticker` kullanmaktır.
`news`'ün taze-Ticker istisnası (T§6.3) buraya genişletilirse maliyet
7'den 16 isteğe çıkar. §9.3 bunu bir testle bağlar.

### 4.5 Kaynak pencere derinlikleri

Geriye dönük çekimin sınırını bu tablo tanımlar.

| dataset | sınır | ölçüm |
|---|---|---|
| `upgrades_downgrades` | **~1000 satır tavanı**, zaman değil | 24 sembolde 1000'i aşan yok; GOOGL 993 (2012-03-14→), AMZN 989 (2020-01-31→), NVDA 985, TSLA 983, AAPL 972. Tavan bağlayıcı olunca geçmiş kırpılır |
| `upgrades_downgrades` güncellik | **Garanti değil** | META 413 satır, **son kayıt 2024-09-30** |
| `insider_transactions` | **150 satır tavanı**, zaman değil | 24 sembolün 12'sinde tam 150. Pencere: JPM 18,7 ay, NVDA 19,8 ay, **WMT 12,4 ay**, **BP.L 12,0 ay**, META ~10 ay |
| `earnings_history` | **tam 4 çeyrek** | 17 sembolde `(4,4)`; mali takvim sembole göre kayar |
| `insider_roster` | 9–10 kişi | işlem tarihleri ~24 ay |
| 13 as-of dataset'i | **pencere yok** | §1 kapsam dışı |

**Sonuç:** `--start 2024-01-01` verildiğinde WMT'nin `insider_transactions`
verisi **hiç gelmez** — kaynak sunmuyor. Aralık daha fazla veri getirmez.

---

## 5. Veri modeli

Motor InnoDB, `utf8mb4` / `utf8mb4_0900_ai_ci`; T§5.1 collation istisnaları
aynen geçerli. Her sembol kolonu `SymbolType()` kullanır.

**as-of** işaretli tablolarda `as_of_date` `DATE NOT NULL`'dır, PK'nın
parçasıdır ve değeri `ctx.fetched_at.date()`'tir (§6.1). Bu tablolara yazma
**her zaman `upsert`**'tir; düz INSERT aynı gün ikinci çalıştırmada
`ERROR 1062` verir (MySQL'de doğrulandı).

**ENUM uyarısı.** Tablolar `utf8mb4_0900_ai_ci` olduğu için ENUM
karşılaştırması da case-insensitive: `'INSTITUTION'` sessizce
`'institution'` olarak yazılır (geçersiz değer ise `ERROR 1265` verir).
`holder_type`, `metric`, `section`, `category` PK bileşenidir; normalizasyon
bu değerleri **daima küçük harfle** üretir, DB'nin sessiz dönüşümüne
güvenilmez.

### 5.1 Analiz tabloları

**`analyst_recommendations`** — PK (`symbol`, `as_of_date`, `period`) · as-of

| Kolon | Tip | Null | Not |
|---|---|---|---|
| `symbol` | `SymbolType()` | H | FK → `symbols`, `ON UPDATE CASCADE ON DELETE RESTRICT` |
| `as_of_date` | `DATE` | H | |
| `period` | `AsciiKeyType(8)` | H | `0m`,`-1m`,`-2m`,`-3m`; satır sayısı 3 veya 4 |
| `strong_buy`, `buy`, `hold`, `sell`, `strong_sell` | `INT UNSIGNED` | H | 19/19 sembolde `int64`, NaN yok |
| `fetched_at` | `TsType()` | H | |

**`analyst_grade_changes`** — PK (`symbol`, `grade_ts_utc`, `firm`) · **as-of yok, saf upsert**

| Kolon | Tip | Null | Not |
|---|---|---|---|
| `symbol` | `SymbolType()` | H | FK |
| `grade_ts_utc` | `TsType()` | H | Kaynak tz-naive; `epochGradeDate` saniyesinden üretildiği için **UTC** |
| `firm` | `KeyTextType(64)` | H | Ölçülen max 26. `''` gelirse satır **yazılmaz** + `WARNING` (§8.3) |
| `to_grade`, `from_grade` | `VARCHAR(32)` | E | Ölçülen max 19; `''` → NULL |
| `action` | `AsciiKeyType(16)` | E | `down/init/main/reit/up` — ENUM değil |
| `price_target_action` | `VARCHAR(16)` | E | Ölçülen max 9; `''` → NULL |
| `current_price_target`, `prior_price_target` | `PriceType()` | E | **`0.0` gerçek değerdir** |
| `fetched_at` | `TsType()` | H | |

PK 296 byte (MySQL'de ölçüldü). Anahtar 15 sembol / 8 852 satırda `dup=0`.
Saf upsert — `replace_scope` olsaydı ~1000 satır tavanı dışında kalan eski
kayıtlar her çalıştırmada silinirdi.

**`analyst_price_targets`** — PK (`symbol`, `as_of_date`) · as-of

| Kolon | Tip | Null |
|---|---|---|
| `symbol` | `SymbolType()` | H (FK) |
| `as_of_date` | `DATE` | H |
| `current`, `low`, `high`, `mean`, `median` | `PriceType()` | E |
| `fetched_at` | `TsType()` | H |

THYAO'da `low`(330) > `current`(294) ölçüldü — **tutarlılık kısıtı kurulmaz**.

**`analyst_estimates`** — PK (`symbol`, `as_of_date`, `metric`, `period`) · as-of

| Kolon | Tip | Null | Not |
|---|---|---|---|
| `symbol`, `as_of_date` | — | H | FK |
| `metric` | `ENUM('eps','revenue')` | H | İki dataset, tek tablo |
| `period` | `AsciiKeyType(8)` | H | `0q`,`+1q`,`0y`,`+1y` |
| `avg`, `low`, `high`, `year_ago_value` | `FactValueType()` | E | `DECIMAL(38,10)` |
| `number_of_analysts` | `INT UNSIGNED` | E | Kaynakta float ve NaN |
| `growth` | `PriceType()` | E | Negatif olabilir |
| `currency` | `AsciiKeyType(8)` | E | |
| `fetched_at` | `TsType()` | H | |

`financial_facts`'ten **bilinçli fark:** tamamı NaN olan dönem satırı yine de
yazılır. Orada satırın yokluğu "kalem yok" demekti; burada dönem seti sabit
dörtlüdür ve NULL satır "dönem var, tahmin yok" bilgisini taşır.

**`analyst_eps_trend`** — PK (`symbol`, `as_of_date`, `period`) · as-of

| Kolon | Tip | Null |
|---|---|---|
| `symbol`, `as_of_date`, `period` | — | H (FK, `AsciiKeyType(8)`) |
| `current`, `days_ago_7`, `days_ago_30`, `days_ago_60`, `days_ago_90` | `FactValueType()` | E |
| `currency` | `AsciiKeyType(8)` | E |
| `fetched_at` | `TsType()` | H |

Kolon adı rakamla başlayamayacağı için `7daysAgo` → `days_ago_7`.

**`analyst_eps_revisions`** — PK (`symbol`, `as_of_date`, `period`) · as-of

| Kolon | Tip | Null | Kaynak anahtarı |
|---|---|---|---|
| `symbol`, `as_of_date`, `period` | — | H | |
| `up_last_7d` | `INT UNSIGNED` | E | `upLast7days` |
| `up_last_30d` | `INT UNSIGNED` | E | `upLast30days` |
| `down_last_7d` | `INT UNSIGNED` | E | **`downLast7Days`** — D büyük |
| `down_last_30d` | `INT UNSIGNED` | E | `downLast30days` |
| `currency` | `AsciiKeyType(8)` | E | |
| `fetched_at` | `TsType()` | H | |

`downLast7days` küçük `d` ile yazılırsa kolon sessizce hep NULL kalır;
dokümantasyon dördünü de küçük yazıyor, kaynak 19/19 sembolde büyük.

**`analyst_growth_estimates`** — PK (`symbol`, `as_of_date`, `period`) · as-of

| Kolon | Tip | Null | Not |
|---|---|---|---|
| `symbol`, `as_of_date` | — | H | FK |
| `period` | `AsciiKeyType(8)` | H | `0q`,`+1q`,`0y`,`+1y`,**`LTG`** |
| `stock_trend`, `index_trend` | `PriceType()` | E | |
| `industry_trend`, `sector_trend` | `PriceType()` | E | 19 sembolde hiç gelmedi; modül istendiği için kolon şimdiden açılır |
| `fetched_at` | `TsType()` | H | |

`index_trend` tüm sembollerde aynıdır (piyasa endeksi trendi); sembol başına
denormalize saklanması bilinçlidir — tek kolon için ayrı bir piyasa tablosu
karşılığı olmayan bir soyutlama olurdu.

**`earnings_history`** — PK (`symbol`, `quarter_end`) · **as-of yok, saf upsert**

| Kolon | Tip | Null | Not |
|---|---|---|---|
| `symbol` | `SymbolType()` | H | FK |
| `quarter_end` | `DATE` | H | tz dönüşümü **yapılmaz** — mali çeyrek etiketi, an değil |
| `eps_actual`, `eps_estimate`, `eps_difference`, `surprise_percent` | `PriceType()` | E | |
| `fetched_at` | `TsType()` | H | |

### 5.2 Sahiplik ve insider tabloları

**`holder_breakdown`** — PK (`symbol`, `as_of_date`) · as-of

| Kolon | Tip | Null | Not |
|---|---|---|---|
| `symbol`, `as_of_date` | — | H | FK |
| `insiders_pct_held`, `institutions_pct_held`, `institutions_float_pct_held` | `PriceType()` | E | |
| `institutions_count` | `INT UNSIGNED` | E | Kaynakta float (`7750.0`) |
| `fetched_at` | `TsType()` | H | |

**`institutional_holders`** — PK (`symbol`, `as_of_date`, `holder_type`, `holder`) · as-of

| Kolon | Tip | Null | Not |
|---|---|---|---|
| `symbol`, `as_of_date` | — | H | FK |
| `holder_type` | `ENUM('institution','mutualfund')` | H | Normalizasyon küçük harf üretir |
| `holder` | `KeyTextType(128)` | H | Ölçülen max 70 |
| `date_reported` | `DATE` | **E** | Satır bazında değişiyor; **hiç boş gelmediği ölçülmedi** → NULL kabul eder (NOT NULL olsaydı tek bir `NaT` sembolün tüm transaction'ını düşürürdü) |
| `pct_held`, `pct_change` | `PriceType()` | E | |
| `shares`, `value` | `BigNumType()` | E | Ölçülen max `1,94×10⁹` / `1,76×10¹³` |
| `fetched_at` | `TsType()` | H | |

PK 548 byte (ölçüldü). `mode="replace_scope"`,
`scope_columns=("symbol","as_of_date","holder_type")`, **`scope_values`
açıkça verilir** (§7.2). MySQL'de doğrulandı: komşu `holder_type` satırlarına
dokunulmuyor.

**`insider_activity`** — PK (`symbol`, `as_of_date`) · as-of

| Kolon | Tip | Null | Not |
|---|---|---|---|
| `symbol`, `as_of_date` | — | H | FK |
| `period_label` | `AsciiKeyType(8)` | H | Başlıktan `Insider Purchases Last (\S+)` ile; 19/19'da `6m` |
| `purchases_shares`, `sales_shares`, `net_shares`, `total_insider_shares` | `BigNumType()` | E | **Negatif olabilir** (KO `-547 806`) |
| `purchases_trans`, `sales_trans`, `net_trans` | `INT` | E | **Signed** — `net_trans` mantıken negatif olabilir ve ölçülmedi; `INT UNSIGNED` olsaydı `ERROR 1264` sembolü düşürürdü |
| `net_pct`, `buy_pct`, `sell_pct` | `PriceType()` | E | |
| `fetched_at` | `TsType()` | H | |

**`insider_transactions`** — PK (`symbol`, `start_date`, `fact_hash`) · **as-of yok, saf upsert**

| Kolon | Tip | Null | Not |
|---|---|---|---|
| `symbol` | `SymbolType()` | H | FK |
| `start_date` | `DATE` | H | |
| `fact_hash` | `ShortHashType()` | H | `(insider, position, text, shares, value, ownership)` kanonik JSON'unun SHA-256'sının ilk 16 hanesi |
| `insider` | `PersonNameType()` | E | Ölçülen max 33; **her zaman kişi adı değil** (`'Elliott Investment Management L.P'`) |
| `position` | `KeyTextType(64)` | E | Ölçülen max 56 (`'Beneficial Owner of more than 10% of a Class of Security'`); `''` → NULL |
| `text` | `VARCHAR(255)` | E | Ölçülen max 79 |
| `transaction_label` | `VARCHAR(64)` | E | 1 464 satırın hepsinde `''` → NULL |
| `url` | `TEXT` | E | 1 464 satırın hepsinde `''` → NULL |
| `shares`, `value` | `BigNumType()` | E | DIS ve BP.L'de `value` tüm satırlarda NaN |
| `ownership` | `AsciiKeyType(8)` | E | `D`, `I`, **`D/I`** (XOM) |
| `fetched_at` | `TsType()` | H | |

**Tekilleştirme zorunludur.** `fact_hash` tek başına yetmiyor: PFE'de dokuz
kolonun tamamında özdeş iki satır ölçüldü, hash'leri de özdeş.
Normalizasyon **önce birebir tekilleştirme** (`drop_duplicates`) yapar, sonra
`fact_hash` üretir; `rows_attempted` tekilleştirme **sonrası** sayıdır, böylece
§8.5 doğrulaması kırılmaz. Düşen satır sayısı `WARNING: dropped N duplicate
insider rows` olarak loglanır. F§8.3'ün `earnings_dates` kuralının aynısı.

`text` kolonunun DB collation'ı `utf8mb4_0900_ai_ci`'dir (PK'da değil);
`fact_hash` **Python tarafında ham dizeden** hesaplandığı için DB'de
`'Sale'`/`'sale'` aynı görünse de hash'te ayrışır. Bu bilinçlidir.

**`insider_roster`** — PK (`symbol`, `as_of_date`, `name`) · as-of

| Kolon | Tip | Null | Not |
|---|---|---|---|
| `symbol`, `as_of_date` | — | H | FK |
| `name` | `PersonNameType()` | H | Ölçülen max 31 |
| `position` | `KeyTextType(64)` | E | Ölçülen max 56 |
| `url` | `TEXT` | E | |
| `most_recent_transaction` | `VARCHAR(64)` | E | Ölçülen max 45 |
| `latest_transaction_date`, `position_direct_date`, `position_indirect_date` | `TsType()` | E | `datetime64` **veya** ham epoch `float64` |
| `shares_owned_directly`, `shares_owned_indirectly` | `BigNumType()` | E | |
| **`position_summary`** | `BigNumType()` | E | NVDA'da yalnız bu kolon dolu; olmasaydı o satırın **tüm** hisse alanları NULL kalırdı |
| **`position_summary_date`** | `TsType()` | E | Kaynakta `float64` epoch |
| `fetched_at` | `TsType()` | H | |

PK 1 055 byte (ölçüldü). `mode="replace_scope"`, kapsam
`("symbol","as_of_date")`, `scope_values` açıkça verilir.
Kaynak kolon seti **7, 9 veya 11**; sıra da sabit değil → `row.get(...)`.

### 5.3 Fon tabloları

**`fund_profile`** — PK (`symbol`, `as_of_date`) · as-of

| Kolon | Tip | Null | Not |
|---|---|---|---|
| `symbol`, `as_of_date` | — | H | FK |
| `quote_type` | `AsciiKeyType(16)` | H | `ETF` / `MUTUALFUND` |
| `category_name` | `VARCHAR(64)` | E | Ölçülen max 25 |
| `family` | `VARCHAR(128)` | E | Ölçülen max 34 |
| `legal_type` | `VARCHAR(64)` | E | Ölçülen max 20; VFIAX/FCNTX'te `None` |
| `description` | `TEXT` | E | Ölçülen max 555 (ARKK) |
| `expense_ratio`, `holdings_turnover`, `total_net_assets` | `PriceType()` | E | `fund_operations` 0. kolonu |
| `expense_ratio_cat`, `holdings_turnover_cat`, `total_net_assets_cat` | `PriceType()` | E | `Category Average` kolonu |
| `cash_position`, `stock_position`, `bond_position`, `preferred_position`, `convertible_position`, `other_position` | `PriceType()` | E | 10/10 fonda aynı 6 anahtar |
| `raw_json` | `RawJsonType()` | H | Sekiz alt yapının kanonik gövdesi |
| `fetched_at` | `TsType()` | H | |

Ölçülen satır boyutu **~1 325 byte** (65 535 bütçesinin %2'si).

**`fund_metrics`** — PK (`symbol`, `as_of_date`, **`section`**, `metric`) · as-of

| Kolon | Tip | Null | Not |
|---|---|---|---|
| `symbol`, `as_of_date` | — | H | FK |
| `section` | `ENUM('equity','bond')` | H | **PK'da** |
| `metric` | `AsciiKeyType(32)` | H | `price_to_earnings`, `price_to_book`, `price_to_sales`, `price_to_cashflow`, `median_market_cap`, `three_year_earnings_growth` (equity); `duration`, `maturity`, `credit_quality` (bond) |
| `value`, `category_average` | `FactValueType()` | E | |
| `fetched_at` | `TsType()` | H | |

`section` PK dışında bırakılırsa aynı `metric` adı iki bölümde geldiğinde
`ERROR 1062: Duplicate entry 'SPY-2026-09-04-price_to_earnings'` alınır
(MySQL'de doğrulandı). Bugünkü 9 ad çakışmıyor ama bunu garanti eden şey
yalnızca Yahoo'nun ad seçimidir — kardeş `fund_weightings` zaten `category`'yi
PK'ya koyuyor. `mode="replace_scope"`, kapsam `("symbol","as_of_date")`.

**`fund_weightings`** — PK (`symbol`, `as_of_date`, `category`, `item_key`) · as-of

| Kolon | Tip | Null | Not |
|---|---|---|---|
| `symbol`, `as_of_date` | — | H | FK |
| `category` | `ENUM('sector','bond_rating')` | H | |
| `item_key` | `AsciiKeyType(32)` | H | `technology`, `us_government`, `below_b`… |
| `weight` | `PriceType()` | H | 10 fonun hepsinde dolu ölçüldü |
| `fetched_at` | `TsType()` | H | |

EAV ölçümle zorunlu. `asset_classes` buraya **girmez** — 6 anahtarı 10/10
fonda sabit olduğu için `fund_profile`'da tipli kolondur.
`mode="replace_scope"`, kapsam `("symbol","as_of_date","category")`.

**`fund_top_holdings`** — PK (`symbol`, `as_of_date`, `holding_symbol`) · as-of

| Kolon | Tip | Null | Not |
|---|---|---|---|
| `symbol`, `as_of_date` | — | H | FK |
| `holding_symbol` | `SymbolType()` | H | **FK YOK** (§5.5); ölçülen max 9 |
| `holding_name` | `KeyTextType(128)` | E | Ölçülen max 51 |
| `holding_percent` | `PriceType()` | E | |
| **`holding_rank`** | `TINYINT UNSIGNED` | H | Kaynak sırası 0–9. Ad `rank` **olamaz**: MySQL 8'de ayrılmış sözcük (`ERROR 1064`) |
| `is_known` | `BOOLEAN` | H | `symbols` tablosunda var mı |
| `fetched_at` | `TsType()` | H | |

`mode="replace_scope"`, kapsam `("symbol","as_of_date")`.
10 fonda `holding_symbol` `dup=0`. BND ve TLT'de 0 satır, AGG'de 1 → `empty`.

### 5.4 `asof_state` — kapı tablosu

**`asof_state`** — PK (`symbol`, `dataset`)

| Kolon | Tip | Null | Not |
|---|---|---|---|
| `symbol` | `SymbolType()` | H | FK → `symbols`, `ON DELETE RESTRICT` |
| `dataset` | `AsciiKeyType(32)` | H | |
| `as_of_date` | `DATE` | H | Son **değişimin** as-of günü |
| `content_hash` | `HashType()` | H | §6.1 |
| `row_count` | `INT UNSIGNED` | H | Denetim; kapı kararında kullanılmaz |
| `first_seen_at` | `TsType()` | H | İlk INSERT'te yazılır, **bir daha güncellenmez** |
| `fetched_at` | `TsType()` | H | **Son doğrulama** zamanı; hash eşitse de güncellenir |

Hash'in veri tablolarında değil burada durmasının nedeni: `funds_data` dört
tabloya yazıyor; hash'i her satıra kopyalamak dört ayrı doğruluk kaynağı
yaratır ve "bu dataset en son ne zaman değişti / kontrol edildi" sorularını
cevaplanamaz kılar.

### 5.5 FK kapsamı

Sembol FK'sı (`ON UPDATE CASCADE ON DELETE RESTRICT`) **18 tablonun
tamamında** vardır. `fund_top_holdings` bu FK'ya **ek olarak** ikinci bir
sembol kolonu taşır ve o kolonda FK **yoktur**:

| Kolon | Gerekçe |
|---|---|
| `fund_top_holdings.holding_symbol` | Kaynakta evren dışı semboller geliyor: `BRK-B`, `AVGO`, yabancı borsa (`2330.TW`, `005930.KQ`, `0700.HK`) ve hatta **fon sembolleri** (`VRTPX`, `BISXX`). FK olsaydı sembol başına tek transaction gereği **fonun tüm verisi rollback olurdu** — `news_symbols` ile aynı gerekçe (T§5.5). `is_known` bayrağı bağı işaretler |

**"Relationları sembol kodu üzerinden kur" bu tabloda karşılanır:** fon →
bileşen sembol ilişkisi; diğer 17 tablo `symbols.symbol` üzerinden bağlıdır.

MySQL'de doğrulandı: `ON UPDATE CASCADE` çocuk tabloya yansıyor,
`DELETE` → `ERROR 1451`, uyumsuz collation → `ERROR 3780`, bilinmeyen sembol
→ `ERROR 1452`. Bileşik FK **yoktur**, dolayısıyla ebeveynde ek indeks
gerekmez.

`yfin symbols purge --force` yeni tabloları kendiliğinden kapsar:
`models/__init__.symbol_scoped_tables()` listeyi `Base.metadata`'dan türetir.

### 5.6 `sync_runs` değişikliği

`sync_runs`'a `selector` `VARCHAR(255)` NULL kolonu eklenir; çalıştırmanın
sembol evrenini ve tarih aralığını insan-okunur biçimde kaydeder
(`exchange=IST quote_type=EQUITY start=2020-01-01`). `scope` kolonu yalnızca
`symbols`/`market` ayrımını taşıyor; hangi run'ın hangi evreni kapsadığı aksi
hâlde bilinemez ve eksiksizlik iddiası denetlenemez.

`server_default` **yoktur** — mevcut satırlar NULL kalır, anlamı "filtresiz".
Değeri `open_run()` yazar, CLI değil (§6.5).

### 5.7 Tip kararları

- **`FactValueType()` = `DECIMAL(38,10)`** yalnızca `analyst_estimates`,
  `analyst_eps_trend`, `fund_metrics` için. Round-trip kaybı yok
  (`1.97656` → `1.9765600000`, `1285436390920.1234567890` tam).
  **11. ondalık basamak sessizce yuvarlanır** — strict modda bile yalnızca
  `Note 1265`, hata değil. Yuvarlama Python tarafında `quantize` ile bilinçli
  yapılır. Tamsayı kısmı 28 haneyi aşarsa gerçek hata (`ERROR 1264`).
- **`BigNumType()` = `DECIMAL(38,0)`** negatif değeri kabul eder (`-547806`
  doğrulandı) ama **kesirliyi round-half-up ile yuvarlar** (`1.9 → 2`,
  yalnız `Note 1265`). Kaynak `Float64` gönderdiğinde yuvarlama Python
  tarafında yapılır (`kinds.py::_to_big` zaten `quantize` uyguluyor).
- **`PriceType()` = `DECIMAL(28,12)`**: `0.0` ve `0` ikisi de `0E-12`, eşit ve
  NULL'dan ayrı — §4.1'in "sıfır gerçek değerdir" kararı DB'de karşılanıyor.
  Tamsayı kapasitesi 16 hane (`1e16` → `ERROR 1264`); fon toplam varlıkları
  için fazlasıyla yeterli.
- **Tüm damgalar `DATETIME(6)`** (T§5.4).
- **`raw_json` yalnızca `fund_profile`'da.** Diğer 17 tablonun kaynak yapısı
  ölçülerek sabit; `funds_data`'nın sekiz alt yapısı EAV'a indirgenirken
  kategori bilgisi kaybolabileceği için gövde korunur.
- **Ayrılmış sözcükler.** `rank` MySQL 8'de window fonksiyonu olarak rezerve
  (`ERROR 1064`) → `holding_rank`. Denenen diğer kolon adları (`mean`,
  `median`, `low`, `high`, `avg`, `value`, `text`, `action`, `position`,
  `period`, `section`, `current`, `growth`) backtick'siz sorunsuz.

### 5.8 İndeksler

- `analyst_grade_changes (firm)`, `(grade_ts_utc)`
- `insider_transactions (start_date)`
- `earnings_history (quarter_end)`
- `institutional_holders (holder)`, `(date_reported)`
- **`fund_top_holdings (holding_symbol)`** — FK olmadığı için InnoDB
  kendiliğinden açmaz **ve** kolon PK'nın son bileşeni olduğu için tek başına
  aranamaz; `SHOW INDEX` ile doğrulandı
- Her as-of tablosunda `(as_of_date)`
- `asof_state (dataset, as_of_date)`

**Bilinçli olarak eklenmeyenler.** `(symbol, grade_ts_utc DESC)` ve
`(symbol, start_date DESC)` ilgili PK'ların **öneki**; `EXPLAIN` optimizer'ın
indeks varken bile `PRIMARY`'yi `Backward index scan` ile seçtiğini gösterdi.
`(metric, period)` kardinalitesi 2×4 olduğu için seçici değil. Üçü de yazma
maliyeti getirip karşılık vermezdi.

### 5.9 View

Yeni view **eklenmez**. "En güncel satır" sorgusu
`WHERE as_of_date <= :d ORDER BY as_of_date DESC LIMIT 1` biçimindedir ve
`(symbol, as_of_date)` PK öneki bunu karşılar; `v_*_latest` deseni 14 as-of
tablosu için 14 view demek olurdu (YAGNI).

---

## 6. Mimari

### 6.1 `AsOfDataset` — üçüncü hash kapısı (`datasets/asof_base.py`)

Kod tabanında iki hash kapısı zaten var; `AsOfDataset` **üçüncü kardeştir**
ve neden diğer ikisinin kullanılamadığı, `hash_gated.py`'nin kendi
docstring'indeki üslupla dosya başına yazılır:

| Taban | Karşılaştırılan / yazılan | Neden burada kullanılamaz |
|---|---|---|
| `SnapshotDataset` | Karşılaştırılan tablo (`snapshot_table`) ile yazılan tablo (`history_table`) **farklı** | Burada kapı ile veri tabloları farklı ama kapı **veri tablosu değil**; ayrıca 1–4 hedef tablo var |
| `HashGatedDataset` | Kapı ile çocuk **aynı anahtar uzayında** (`financial_periods` → `financial_facts`); çocuk silme kapsamı `gate_key_columns`'tan türetilir | Kapı anahtarı `(symbol, dataset)`, çocukların böyle bir kolonu **yok**; silme kapsamı çocuğun kendi `scope_columns`'ıdır |

Paylaşılan ilke aynen korunur: **kapı satırı her durumda yazılır**, hash
eşitse yalnız `fetched_at` (`hash_gated.UNCHANGED_UPDATE_COLUMNS` yeniden
kullanılır).

```python
# Hash'ten DISLANAN kolonlar: ikisi de her calistirmada degisir; govdeye
# girselerdi hash hicbir zaman esitlenmez ve mekanizma sessizce hic
# calismazdi (her gun her satir yeniden yazilirdi).
VOLATILE_COLUMNS = frozenset({"as_of_date", "fetched_at"})

GATE_TABLE = "asof_state"
GATE_KEY_COLUMNS = ("symbol", "dataset")


class AsOfDataset[RawT](Dataset[RawT]):
    """as_of_date PK'da; content_hash degismediyse VERI tablolarina yazilmaz."""

    def upsert(self, writer: RowWriter, result: NormalizedResult) -> WriteStats: ...
```

**Hash tanımı — tek ve tam:**

```
payload = [
    {"table": w.table,
     "rows": sorted(
         ({k: v for k, v in row.items() if k not in VOLATILE_COLUMNS}
          for row in w.rows),
         key=lambda r: tuple(str(r.get(c)) for c in w.key_columns),
     )}
    for w in sorted(result.writes, key=lambda w: w.table)
]
content_hash = sha256(nz.canonical_json(payload))
```

**Satırların sıralanması zorunludur.** `nz.canonical_json` yalnız sözlük
anahtarlarını sıralar (`sort_keys=True`, `normalize.py:317`); liste sırası
korunur. Kaynak (Yahoo'nun "ilk 10 kurum" listesi, `insider_roster`) sırayı
değiştirdiğinde içerik aynıyken hash değişir ve mekanizma her gün gereksiz
yazım yapardı — varlık nedeninin tam tersi.
`Decimal`, `date` ve `Timestamp` değerleri `YFJSONEncoder` tarafından kanonik
dizeye indirilir (`normalize.py:283-286`), `NaN` `_scrub_nan` ile `None`'a
düşer; hash Python sürümünden bağımsızdır.

**Akış:**

1. `normalize` satırları üretir; her satıra `as_of_date = ctx.fetched_at.date()`
   ve `fetched_at = ctx.fetched_at` yazılır. Kapı satırını `normalize`
   **üretmez** — hash `result.writes`'tan türetildiği için kendini içeren bir
   yazım döngüsel olurdu.
2. `upsert` `writer.current_hash("asof_state", {"symbol": s, "dataset": self.name})`
   okur. **Mevcut protokol yeterlidir**, `RowWriter`'a metot eklenmez.
3. **Sonuç boşsa** (`result.is_empty`) kapı satırı **yazılmaz** ve hiçbir şey
   yapılmaz; hücre `empty` olur. Aksi hâlde her fon-olmayan sembol için
   `asof_state`'te ölü satır birikir ve `first_seen_at` "ilk kez boş dönüldü"
   anlamına kayardı.
4. Hash **aynıysa**: veri tablolarına yazılmaz; satır taşıyan her hedef tablo
   için `stats.skipped[tablo] += len(rows)`; kapı satırı
   `update_columns=("fetched_at",)` ile yazılır.
5. Hash **farklıysa**: tüm `TableWrite`'lar uygulanır ve kapı satırı tam
   yazılır. `update_columns` **`first_seen_at` içermez** — aksi hâlde
   `ON DUPLICATE KEY UPDATE` §5.4'ün "ilk INSERT'te yazılır" kuralını bozar:

```python
TableWrite(
    table="asof_state",
    rows=[{"symbol": s, "dataset": self.name, "as_of_date": d,
           "content_hash": h, "row_count": n,
           "first_seen_at": ctx.fetched_at, "fetched_at": ctx.fetched_at}],
    key_columns=GATE_KEY_COLUMNS,
    update_columns=("as_of_date", "content_hash", "row_count", "fetched_at"),
)
```

Hepsi sembolün **tek transaction'ı** içindedir (T§8.7): yazma başarısız olup
hash güncellenirse bir sonraki çalıştırma "değişmedi" deyip eksik veriyi
kalıcılaştırırdı.

**`produces` kapı tablosunu içerir.** 13 as-of dataset'inin `produces`
değeri `("<hedef tablo(lar)>", "asof_state")`'tir. `produces` sözleşmesi
"yazdığı tablo adları"dır ve `tests/unit/test_registry.py:101-113` her
elemanın `Base.metadata.tables`'ta bulunmasını zorunlu kılıyor; bildirmemek
`_failed_records`'ın (`runner.py:266`) hata durumunda kapı satırını
denetimden düşürmesine ve `produces` ile fiili çıktının ayrışmasına yol
açardı. Sonuç: as-of dataset'leri `sync_run_items`'a hedef tablo sayısı + 1
satır yazar.

**Aynı gün ikinci çalıştırma.** Hash değişmişse satırlar aynı `as_of_date`
ile üstüne yazılır (upsert); farklı bir satır oluşmaz (MySQL'de doğrulandı).
`as_of_date` günlük granülariteyi bilinçli olarak sabitler.

**`--full-refresh` bu mantığı etkilemez** — T§7.3'teki `content_hash`
kararıyla tutarlı. Zorlama gerekirse `asof_state` satırı silinir.

### 6.2 `Dataset.date_range` ve `SyncContext.start/end`

```python
DateRange = Literal["api", "filter", "none"]

class Dataset[RawT](ABC):
    ...
    date_range: DateRange = "none"
```

| Seviye | Anlamı | Dataset'ler |
|---|---|---|
| `"api"` | `fetch` aralığı **yfinance çağrısına geçirir** — gerçek geriye dönük çekim | Mevcut hat: `history`, `shares_full`, `dividends`, `splits`, `capital_gains` (§10/12) |
| `"filter"` | Kaynak sabit pencere döndürür; `normalize` kaynak tarih kolonuna göre satır eler | `upgrades_downgrades` (`grade_ts_utc`), `earnings_history` (`quarter_end`), `insider_transactions` (`start_date`) |
| `"none"` | Aralık anlamsız | Kalan 14 |

Üçüncü seviye bu spec'te kullanılmıyor ama **tanımlanması zorunludur**:
`--start` bayrağı ilk günden itibaren `history` üzerinde de çalışır ve orada
"satır eleme" değil "farklı çekim" anlamına gelir. İki davranışı tek bir
`bool` ile ayırmak, `--start 2020-01-01 --datasets history` komutunun
Yahoo'dan 2020 öncesini çekip **sonra atmasına** yol açardı.

`SyncContext` iki alan kazanır: `start: date | None`, `end: date | None`.
Varsayılan `None`/`None` — **filtresiz çalıştırma Yahoo'nun verdiği tüm
geçmişi yazar**.

### 6.3 Kayıtlı dataset'ler

17 kayıt, hepsinin `depends_on = ("symbols",)` değeri var.

| `name` | yfinance çağrısı | `produces` | `date_range` |
|---|---|---|---|
| `recommendations` | `get_recommendations()` | `analyst_recommendations`, `asof_state` | `none` |
| `upgrades_downgrades` | `get_upgrades_downgrades()` | `analyst_grade_changes` | **`filter`** |
| `analyst_price_targets` | `get_analyst_price_targets()` | `analyst_price_targets`, `asof_state` | `none` |
| `earnings_estimate` | `get_earnings_estimate()` | `analyst_estimates`, `asof_state` | `none` |
| `revenue_estimate` | `get_revenue_estimate()` | `analyst_estimates`, `asof_state` | `none` |
| `eps_trend` | `get_eps_trend()` | `analyst_eps_trend`, `asof_state` | `none` |
| `eps_revisions` | `get_eps_revisions()` | `analyst_eps_revisions`, `asof_state` | `none` |
| `earnings_history` | `get_earnings_history()` | `earnings_history` | **`filter`** |
| `growth_estimates` | `get_growth_estimates()` | `analyst_growth_estimates`, `asof_state` | `none` |
| `sustainability` | `get_sustainability()` | **`()`** | `none` |
| `major_holders` | `get_major_holders()` | `holder_breakdown`, `asof_state` | `none` |
| `institutional_holders` | `get_institutional_holders()` | `institutional_holders`, `asof_state` | `none` |
| `mutualfund_holders` | `get_mutualfund_holders()` | `institutional_holders`, `asof_state` | `none` |
| `insider_purchases` | `get_insider_purchases()` | `insider_activity`, `asof_state` | `none` |
| `insider_transactions` | `get_insider_transactions()` | `insider_transactions` | **`filter`** |
| `insider_roster_holders` | `get_insider_roster_holders()` | `insider_roster`, `asof_state` | `none` |
| `funds_data` | `get_funds_data()` | `fund_profile`, `fund_metrics`, `fund_weightings`, `fund_top_holdings`, `asof_state` | `none` |

**Alias'lar** (`SYMBOL_DATASETS`):
- `recommendations_summary` → `("recommendations",)`
- `analysis` → `recommendations`, `upgrades_downgrades`, `analyst_price_targets`,
  `earnings_estimate`, `revenue_estimate`, `eps_trend`, `eps_revisions`,
  `earnings_history`, `growth_estimates` — **`sustainability` dâhil değildir**
- `holders` → altı sahiplik dataset'i
- `funds` → `("funds_data",)`

**Zorunlu notlar:**

- **`recommendations_summary` kayıt değil, alias'tır** (`base.py:220-221`).
- **`sustainability` bir izleme dataset'idir.** `produces = ()`; `normalize`
  her zaman boş `NormalizedResult` döndürür. Kaynak dolu gelirse
  `WARNING: sustainability now returns data (shape=…, columns=…)`.
  Varsayılan **kapalıdır** (`YF_PROBE_SUSTAINABILITY=0`) — 19 sembolde de 404
  aldığı için §8.4'ün 404 kuralıyla her sembolde bir `empty` hücre ve boşa
  bir istek üretirdi.
- **`funds_data` ön kontrol yapar.** `fetch_fast_info(ctx)`'ten
  `quoteType ∉ {ETF, MUTUALFUND}` ise **hiç istek yapmadan** `empty` döner;
  anahtar yoksa `fetch_history_metadata(ctx)`'ten `instrumentType`'a düşer.
  12 sembolde ikisi birebir aynı ölçüldü ve **aynı chart isteğinden**
  besleniyorlar; ikisi de bootstrap `symbols` dataset'i tarafından zaten
  çekilip `ctx` önbelleğine konmuştur (`datasets/symbols.py:28-46`), bu yüzden
  `depends_on`'a `history_metadata` **eklenmez** — eklenirse
  `--datasets funds` çalıştırması `history_metadata` **tablosuna** da yazar ve
  fazladan denetim satırı üretir.
  Ayrıca `except (YFDataException, KeyError)` yakalanır: `hide_exceptions=False`
  altında kaynak `YFDataException` değil **ham `KeyError('topHoldings')`**
  fırlatıyor (`scrapers/funds.py:190-194`).
- **Yeni bir önbellek katmanı eklenmez.** Modül paylaşımı yfinance'ın kendi
  `Analysis`/`Holders`/`Quote` örnek niteliklerinden geliyor (§4.4); yalnızca
  mevcut `ctx` önbellek anahtarları (`fetch_fast_info`, `fetch_history_metadata`)
  okunur.
- **`institutional_holders` ve `mutualfund_holders` aynı tabloya yazar.**
  Kapsamları `scope_columns=("symbol","as_of_date","holder_type")` ile
  ayrışır; ayrı kayıt kalırlar çünkü ayrı `sync_run_items` hücresi ve ayrı
  `--datasets` seçilebilirliği gerekir.

### 6.4 Sembol seçimi — `--exchange`, `--quote-type`, `--suffix`

`sync` komutuna üç DB filtresi eklenir; `symbols` tablosu üzerinde **AND**'lenir
ve `--symbols` ile birlikte kullanılamaz (hata, çıkış 1).

| Seçenek | Kolon | Gerekçe |
|---|---|---|
| `--exchange` | `symbols.exchange`, virgüllü, `upper()` | Zaten indeksli (T§5.6). Bir piyasa **birden çok koda** dağılır (ABD: `NMS`,`NAS`,`NYQ`,`PCX`) |
| `--quote-type` | `symbols.quote_type`, virgüllü, `upper()` | 17 dataset `CRYPTOCURRENCY`/`CURRENCY`/`FUTURE`/`INDEX`'te boş; §8.4 ile birlikte bu semboller sembol başına 7 boşa istek ve 16 `empty` hücre üretir |
| `--suffix` | `symbol LIKE '%.IS'` | **Borsa çözümlenmeden de çalışır** |

Ölçülen değerler: `NMS`/EQUITY (AAPL, MSFT), `IST`/EQUITY (THYAO.IS,
GARAN.IS), `PCX`/ETF (SPY), `NAS`/MUTUALFUND (VFIAX), `LSE` (BP.L),
`KSC` (005930.KS), `JPX` (7203.T), `EBS` (NESN.SW), `CCC`/CRYPTOCURRENCY,
`CCY`/CURRENCY, `CMX`/FUTURE, `SNP`/INDEX.

**NULL tuzağı.** `exchange` ve `quote_type` kolonlarını bootstrap `symbols`
dataset'i doldurur; `yfin symbols add` ile eklenen sembolde ilk sync'e kadar
NULL'durlar. Filtre uygulandığında CLI elenen sembol sayısını **ayrıca**
bildirir:

```
3 sembol filtre disinda birakildi (exchange NULL - henuz cozulmemis).
Once 'yfin sync --datasets symbols' calistirin ya da --suffix kullanin.
```

**Keşfedilebilirlik:** `yfin symbols exchanges` — farklı
`(exchange, full_exchange_name, quote_type)` üçlülerini sayılarıyla listeler;
`NULL` satırı "çözülmemiş" görünür.

### 6.5 Değişen kod noktaları — "iki satır" değil

`runner.py`'de **altı**, `shard.py`'de **iki**, `cli.py`'de **bir** nokta:

| # | Dosya:satır | Değişiklik |
|---|---|---|
| 1 | `runner.py:214` | `tables = stats.tables() or list(dataset.produces) or [None]` — `produces=()` olan dataset (`sustainability`) bugün `sync_run_items`'a **hiç satır yazmıyor** |
| 2 | `runner.py:266` | `_failed_records`: `tuple(dataset.produces) if dataset else (None,)` → `or (None,)`. Aynı körlük **hata** yolunda da var; `sustainability` ağ hatası aldığında yine denetimden kaybolurdu. Bu fonksiyon `_persist_with_retry`'nin transaction hatası yolunda da kullanılıyor (`runner.py:421-425`) |
| 3 | `runner.py:61-73` | `SymbolPayload`'a `skipped: list[tuple[str, str]]` kanalı (dataset, gerekçe) — bugün yalnız `results` ve `failures` var |
| 4 | `runner.py:183-204` | `_worker`: `--start/--end` verildiğinde `date_range == "none"` dataset'leri **fetch'ten önce** eler, `payload.skipped`'a yazar |
| 5 | `runner.py:279-288` | `_persist_symbol`: `payload.skipped`'ı `ItemRecord(status=SKIPPED, error="date_range=none")` olarak üretir; `table_name` `produces` başına bir satır, `produces` boşsa `NULL` |
| 6 | `runner.py:350-374` | `open_run(...)` `selector` parametresi alır ve `sync_runs.selector`'a yazar. Üç çağrı yeri: `runner.py:781`, `shard.py:252`, `market_runner.py:142` |
| 7 | `shard.py:56-73` | `ShardSpec`'e `start: date \| None`, `end: date \| None`, `selector: str \| None` alanları (frozen, pickle'lanabilir) |
| 8 | `shard.py:99-135` | `shard_main` / `run_shard` bunları `_worker`'a taşır |
| 9 | `cli.py:250` civarı | Beş seçenek → `run_sharded` → `ShardSpec` zinciri |

7–8 **kritiktir**: `full_refresh` bu zincirin her halkasında ayrı ayrı
taşınıyor (`cli.py:250` → `shard.py:222,273,288,331,351` → `runner.py:478,519,160,169`).
`start`/`end` için aynı zincir kurulmazsa proxy havuzu doluyken —
**varsayılan yol** — child process `start=None` ile koşar: `"filter"`
dataset'leri aralığı uygulamaz, `"none"` dataset'leri atlanmaz ve kullanıcı
"aralık uygulandı" sanır.

### 6.6 Yeni API ekleme yolu

Değişmedi: `datasets/` altına tek dosya, `models/` altına tablo, tek Alembic
migration. As-of grameri gereken dataset `AsOfDataset`'ten türer; aralık
desteği `date_range` sınıf niteliğiyle bildirilir.

---

## 7. Veri akışı

### 7.1 Sembol tarafı

```
yfin sync --exchange IST --quote-type EQUITY --datasets analysis
  │
  ├─ Sembol evreni: symbols WHERE is_active=1 AND exchange IN (...) AND quote_type IN (...)
  │     filtre disi birakilanlar (NULL kolon) ayrica raporlanir
  │
  ├─ MySQL advisory lock GET_LOCK('yfin_sync', 0)
  ├─ open_run(scope='symbols', selector='exchange=IST quote_type=EQUITY')
  │
  ├─ Proxy shard'lari (P) / ThreadPoolExecutor — sembol basina TEK yf.Ticker:
  │     recommendationTrend / upgradeDowngradeHistory / financialData /
  │     earningsTrend / earningsHistory / industry+sector+indexTrend /
  │     holders demeti  → 7 istek, 17 dataset
  │
  └─ Ana thread: sembol basina TEK transaction
        AsOfDataset: asof_state hash'i okunur; esitse veri tablolarina
        yazilmaz, kapi satirinin yalniz fetched_at'i guncellenir
        → sync_run_items (tablo basina bir satir)
```

### 7.2 Idempotency ve `replace_scope`

- Tüm yazmalar `INSERT … ON DUPLICATE KEY UPDATE`, `update_columns` ile
  kapsamı sınırlı.
- **`replace_scope` kullanan beş tablo `scope_values`'ı açıkça verir.**
  `MySQLRowWriter._delete_scope` kapsam değerlerini `scope_values`
  verilmemişse **satırlardan** türetiyor (`persistence.py:130-139`); `rows`
  boşsa silme **hiç yapılmaz**. Senaryo: sabah `institutional_holders` 10
  satır yazıldı; akşam kaynak boş döndü. Hash farklı olduğu için kapı açılır,
  `TableWrite(rows=[])` uygulanır, silme yapılmaz ve sabahki satırlar
  bugünün as-of gününde kalıcı olur. `hash_gated.py:72`'deki desenin aynısı
  kullanılır:
  `scope_values=({"symbol": s, "as_of_date": d, "holder_type": t},)`.
  Etkilenen tablolar: `institutional_holders`, `insider_roster`,
  `fund_metrics`, `fund_weightings`, `fund_top_holdings`.
- As-of dataset'lerinde hash eşleşiyorsa veri tablolarına yazma olmaz;
  **satır taşıyan** hedef tablo `skipped`, **boş `TableWrite` taşıyan** hedef
  tablo `empty` kalır (`runner.py:219-222`). Bu yüzden tek bir dataset aynı
  çalıştırmada hem `skipped` hem `empty` hücre üretebilir — BND'de
  `fund_top_holdings` boştur, kardeş üç tablo `skipped` olur.
- Saf upsert dataset'lerinde değişmeyen satır `ROW_COUNT()=0` verir;
  doğrulama **anahtar varlığı sorgusuyla** yapıldığı için hücre yine `ok`
  olur (T§8.6). MySQL'de üç değer de ölçüldü: yeni 1, değişen 2, değişmeyen 0.

### 7.3 Tarih aralığı ve geriye dönük çekim

Yahoo bu 17 uçta tarih parametresi sunmuyor (§4.2). Bu yüzden:

- `"api"` dataset'lerinde `--start`/`--end` **watermark'ı geçersiz kılar**.
- `"filter"` dataset'lerinde aralık `normalize` içinde uygulanır. Ağ maliyeti
  değişmez; kazanç hedefli onarım ve doğrulamadır.
- `"none"` dataset'leri **çalıştırılmaz**; `sync_run_items`'a `skipped` +
  `error="date_range=none"` yazılır (§6.5/4-5).
- Seçilen dataset'lerin **tamamı** `"none"` ise CLI hata verip çıkar (kod 1).
- `--start > --end` → hata. `--end` gelecekte olabilir.

**Aralığın getiremeyeceği veri.** `--start 2024-01-01`, WMT'nin
`insider_transactions` verisini 2024'e uzatmaz: sınır 150 satırdır ve WMT'de
pencere zaten 12,4 aydır (§4.5). `earnings_history` yine 4 çeyrek döndürür.
`upgrades_downgrades` ~1000 satır tavanına takılır ve **güncel olmayabilir**
(META son kayıt 2024-09-30). Analist tahminlerinin geçmişi hiçbir
parametreyle çekilemez; `as_of_date` geçmişi ileriye doğru birikir.
`analyst_eps_trend`'in `days_ago_7/30/60/90` kolonları tek kısmi telafidir.

### 7.4 Rate limiting

Mevcut token bucket (varsayılan 2 istek/sn) ve `tenacity` backoff'u aynen
kullanılır. **P §7.2 ile bu limit artık shard BAŞINADIR**; toplam efektif hız
`shard_count × YF_RATE_LIMIT_PER_SEC`'tir. Sembol başına **7 istek**;
tek shard'da 1 000 sembol ~1 saat, 4 shard ile ~15 dakika. Bütçe kurulurken
P §5.3'ün **bir tenacity denemesi ≥2 gerçek istek** çarpanı da sayılmalıdır. `--datasets analysis` / `holders` ayrımı ve
`--quote-type EQUITY` bu maliyetin somut yönetim araçlarıdır.

---

## 8. Hata yönetimi ve veri bütünlüğü

### 8.1 İzolasyon sınırı

**(sembol × dataset)** hücresi, tablo başına bir `sync_run_items` satırı,
durumlar `ok`/`empty`/`skipped`/`failed`/`unknown_symbol` (T§8.1).

**Paylaşılan istek, paylaşılan kader.** Holders demeti tek istektir; istek
patlarsa altı dataset de `failed` olur. Bu doğru davranıştır ve maliyeti
yoktur — yfinance sonucu (hatayı da) `Ticker` üzerinde önbelleğe aldığı için
altı hücre bir istek harcar. Aynısı `earningsTrend` için geçerlidir.

**`unknown_symbol` ile etkileşim.** Sembol çözülemediğinde `runner.py:575-584`
**tek** bir satır yazar (`dataset="symbols"`); dataset başına satır yazılmaz
ve `asof_state`'e dokunulmaz. Dolayısıyla o run'da 17 dataset denetim kaydı
almaz — bu bilinçlidir, çözülemeyen sembolde hiçbir çağrı yapılmamıştır.

Çıkış kodları **P §8.1'deki hâliyle** geçerlidir: `failed` **ve**
`not_attempted` yok → 0; kısmi → 2; tümü `failed` → 3; kilit alınamadı → 4;
uygun proxy yok → 5. (`ItemStatus.NOT_ATTEMPTED` P §3.6 ile eklendi;
işlenmemiş sembol varken çıkış kodu asla 0 olamaz.)

### 8.2 `empty` ≠ `failed` — ölçülmüş boş durumlar

| Dataset | Boş dönen |
|---|---|
| 17 dataset'in tamamı | `SPY`, `VFIAX`, `BTC-USD`, `^GSPC`, `EURUSD=X`, `GC=F`, uydurma sembol (`funds_data` hariç: SPY ve VFIAX'te **dolu**) |
| `sustainability` | **19 sembolün 19'u** |
| `upgrades_downgrades`, `earnings_history`, `institutional_holders`, `mutualfund_holders`, `insider_transactions`, `insider_roster_holders` | `THYAO.IS`, `NESN.SW` |
| `fund_top_holdings`, `fund_weightings(category='sector')` | `BND`, `TLT` (tahvil fonu) |
| `funds_data` tamamı | fon olmayan her sembol |

Bunların hiçbirine "dolu gelmeli" iddiası **kurulmaz**.

### 8.3 Normalizasyon kuralları (yeni)

| Durum | Kural |
|---|---|
| **Boş sonuç kontrolü** | `raw is None or len(raw) == 0` (T§8.3) |
| **`funds_data` ön kontrolü** | `fast_info['quoteType']` (yoksa `history_metadata['instrumentType']`) `∉ {ETF, MUTUALFUND}` → **hiç istek yapmadan** `empty`; ayrıca `except (YFDataException, KeyError)` |
| **Dinamik kolon adı** | `insider_purchases` 0. kolonu ve `fund_operations`/`equity_holdings`/`bond_holdings` 0. kolonu **konumdan** (`df.iloc[:, 0]`) okunur |
| **`period_label` ayrıştırma** | `Insider Purchases Last (\S+)` → `6m`. `period_label` NOT NULL olduğu için desen tutmazsa satır **yazılmaz**, hücre `failed` olur ve `WARNING: unparsable insider period header: <baslik>` loglanır |
| **Değişken kolon seti ve sırası** | `insider_roster_holders` 7/9/11 kolon, sıra sabit değil → `row.get(...)`; `positionSummary` ve `positionSummaryDate` tipli kolona alınır (§5.2) |
| **Tarih mi float mu** | `Position Direct/Indirect Date` `datetime64` **veya** ham epoch `float64` — 6 sembolde dolu float ölçüldü. `kinds.py::_to_datetime` (`dt` kind'ı) iki biçimi de kabul eder |
| **Birebir tekilleştirme** | `insider_transactions` satırları `drop_duplicates()`'ten geçirilir; PFE'de dokuz kolonun tamamında özdeş iki satır var ve `fact_hash` onları ayıramaz. Düşen sayı `WARNING: dropped N duplicate insider rows` |
| **Sentinel** | `''` → NULL: `ToGrade`, `FromGrade`, `priceTargetAction`, `Transaction`, `URL`, **`Position`** (BP.L'de boş ölçüldü) |
| **`firm` boşsa** | PK bileşeni olduğu için satır **yazılmaz** + `WARNING: empty firm in grade change` — boş string PK'ya girip iki farklı kaydı birleştirirdi |
| **Sıfır ≠ NULL** | `currentPriceTarget = 0.0`, `revenue_estimate.avg = 0`, `bond_ratings.us_government = 0.0` gerçek değerlerdir |
| **`pd.NA` ve dtype kararsızlığı** | `insider_purchases.Shares` ∈ {`Float64`,`Int64`}, `insider_transactions.Value` ∈ {`float64`,`int64`} — sembole göre değişiyor. `nz.is_missing` `pd.NA`'yı kapsıyor (`normalize.py:80`); dönüşüm dtype'a değil değere bakar |
| **DECIMAL yuvarlama** | `DECIMAL(38,10)` 11. basamağı, `DECIMAL(38,0)` kesirli kısmı **sessizce** (yalnız `Note 1265`) yuvarlar. Yuvarlama Python tarafında `quantize` ile bilinçli yapılır (`kinds.py`) |
| float → DECIMAL | `Decimal(repr(float(x)))` (T§8.3) |
| `grade_ts_utc` | Kaynak tz-naive ama `epochGradeDate` saniyesinden üretilmiş → **UTC kabul edilir**, ikinci dönüşüm yapılmaz |
| `quarter_end`, `date_reported`, `start_date` | tz-naive `Timestamp` → `.date()`; **tz dönüşümü yapılmaz** (takvimsel etiket) |
| `eps_revisions` anahtarları | `upLast7days`, `upLast30days`, `downLast30days`, **`downLast7Days`** |
| ENUM değerleri | Normalizasyon **daima küçük harf** üretir; DB'nin `ai_ci` sessiz dönüşümüne (`'INSTITUTION'` → `'institution'`) güvenilmez |
| Sembol normalizasyonu | `holding_symbol` dâhil tüm sembol alanları `strip().upper()` |
| **Aşırı uzun metin** | `firm`, `holder`, `name`, `item_key`, `metric`, `ownership` sınırı aşarsa satır **yazılmaz**, hücre **`ok`** kalır (F§8.4 deseni; `common.key_value` docstring'i: *"tek bozuk anahtar yüzünden aynı çağrının geçerli satırlarını da kaybetmemek için hücre `failed` yapılmaz"*), `WARNING: <alan> too long: <deger> (<n> karakter)` (F§8.4) |
| **`sustainability` izleme** | Boşsa sessiz; dolu gelirse `WARNING: sustainability now returns data (…)` |
| Yeni alan kaçırmama | `analyst_grade_changes`, `institutional_holders`, `insider_roster`, `fund_profile` normalizasyonu `datasets/common.warn_unmapped` ile haritalanmamış kolonları loglar |

### 8.4 `hide_exceptions = False` **+ HTTP 404 → `empty`**

İki kural birlikte uygulanır; **ayrı ayrı ikisi de yanlıştır**.

**Neden ayar gerekli.** `Holders._fetch_and_parse` HTTPError yakaladığında
yedi çerçevenin hepsini boş `DataFrame`'e set edip **sessizce dönüyor**
(`holders.py:71-84`). Simüle edilmiş 503 ile doğrulandı:

```
hide_exceptions=True   → major_holders shape=(0,0)      (6 property'nin hepsi SESSIZ BOS)
hide_exceptions=False  → major_holders HTTPError 503    (6 property'nin hepsi)
```

Varsayılan ayarla ağ hatası altı hücrede `failed` değil **`empty`** görünür —
yani veri kaybı denetim kaydında "kaynakta veri yok" olarak normalleşir ve
§8.2'nin ayrımı sessizce çürür.

**Neden ayar tek başına yetmez.** Aynı ayar Yahoo'nun "bu sembolde bu modül
yok" 404'lerini de açar. Canlı ölçüm:

```
hide_exceptions=False:
  ^GSPC        → 16 dataset'in TAMAMI  HTTPError 404
  ZZZZFAKE     → 16 dataset'in TAMAMI  HTTPError 404
  THYAO.IS     → upgrades_downgrades   HTTPError 404   (gecerli bir EQUITY)
  AAPL         → sustainability        HTTPError 404   (HER sembolde)
```

Bu hâliyle ayar §4.1'in "egzotik sembolde exception yok", §4.3'ün matrisi,
§8.2'nin boş listesi ve §6.3'ün "`sustainability` boşsa sessiz" tanımını
birden çürütür ve her run'ı kalıcı olarak `partial` (çıkış 2) yapar.

**Kural — mekanizma kodda hazır.** `client.py` süreç başında
`hide_exceptions = False` ve `retries = 0` yapıyor (`client.py:149-153`), ve
404 ayrımı **uç seviyesinde**, `errors.is_absent_data()` +
`client.call_optional(fn, what=...)` ile çözülmüş durumda: sarmalayıcı
"veri yok" durumunda `None` döndürür, diğer her hatayı olduğu gibi yükseltir.

| İstisna | Davranış |
|---|---|
| HTTP **404** | `call_optional` → `None` → `empty` |
| `IndexError("positional indexers are out-of-bounds")` | `None` → `empty` (yfinance trailing veriyi boş `.iloc` ile okur) |
| HTTP 429 / 401 / 403 / ≥500 / timeout | yükselir → `failed` (+ proxy cezası, P) |
| `KeyError` / `TypeError` / ayrıştırma hatası | yükselir → `failed`, **`empty` değil** |

**Bu spec'in 17 dataset'inin `fetch`'i `call_optional` kullanır** — hepsinde
yokluk meşrudur (§8.2). `runner.py`'nin hata → durum eşlemesi **değişmez**:
ayrım uçta, açık bir çağrıyla yapılır. Runner seviyesinde küresel bir kural,
yokluğu meşru olmayan bir uçta (ör. `symbols` bootstrap'i) 404'ü de sessizce
`empty`'ye çevirirdi; opt-in sarmalayıcı bu riski taşımaz.

Kural **dar tutulur**: yalnızca gerçek HTTP 404 ve tek bir bilinen `IndexError`
mesajı `empty`'ye düşer. `ErrorKind.DATA` sınıfının tamamını `empty` saymak bir
ayrıştırma hatasını "veri yok" olarak maskelerdi.

### 8.5 Reconciliation

Değişmez: `rows_verified == rows_attempted`, anahtar varlığı sorgusuyla
(T§8.6); `rows_skipped` ayrı sayaçta.

`insider_transactions`'ta `rows_attempted` **birebir tekilleştirme
sonrası** sayıdır (§8.3). Tekilleştirme yapılmasaydı PFE'de 34 satır okunup
33 yazılır ve eşitlik her çalıştırmada kırılırdı.

As-of dataset'lerinde hash eşleştiğinde `attempted = 0`, `skipped = n` olur;
`runner.py:219-226` bunu zaten `SKIPPED` olarak sınıflandırıyor.

Bileşik anahtarlı tablolarda mevcut `verify_rows` 500'lük parçalara böler;
AAPL'in 972 grade change'i 2 sorguya iner.

### 8.6 Transaction ve eşzamanlılık

Sembol başına tek transaction, `GET_LOCK('yfin_sync', 0)` (T§8.7).
`asof_state` güncellemesi veri yazımlarıyla **aynı transaction'dadır**.

### 8.7 Sembol silme

18 tablonun tamamı `ON DELETE RESTRICT` taşır; `yfin symbols purge --force`
listeyi `Base.metadata`'dan türettiği için yeni tabloları kendiliğinden
kapsar. `fund_top_holdings.holding_symbol` FK taşımaz; bir fonun bileşeni
olan sembolün silinmesini engellemez, `is_known` bir sonraki çalıştırmada
`0`'a düşer.

---

## 9. Test stratejisi

### 9.1 Normalizasyon testleri (fixture, ağsız)

Referans semboller:

| Sembol | Kapsadığı kenar durum |
|---|---|
| `AAPL` | Tam kapsam; 972 grade change; `value` NaN'lı insider satırları |
| `MSFT` | `(Start Date, Insider, Text)` çakışması — `fact_hash` gerekçesi |
| **`PFE`** | **Dokuz kolonun tamamında özdeş iki satır** — birebir tekilleştirme gerekçesi |
| **`XOM`** | `Ownership = 'D/I'` — `AsciiKeyType(8)` gerekçesi |
| **`NVDA`** | `insider_roster` **11 kolon**, `positionSummary` dolu, `Position Indirect Date` float epoch |
| **`WMT`** | `position` 56 karakter; `insider_transactions` 150 satır / 12,4 ay |
| `THYAO.IS` | `currency='TRY'`; `recommendations` 3 satır; 6 dataset boş; `0q`/`+1q` NaN; `revenue_estimate.avg = 0` |
| `KO` | `insider_activity`'de negatif `net_shares`; roster'da dolu float tarih |
| `SPY` | ETF: `funds_data` dolu, 11 sektör + 1 rating |
| `BND` | Tahvil ETF'i: 0 sektör + 9 rating, `top_holdings` **boş** |
| `^GSPC` | 17 dataset boş |

Kural başına test, özellikle:

- `downLast7Days` anahtarının **fixture'dan** okunduğu — küçük `d` ile
  yazılmış bir test geçer ve kolon sessizce NULL kalırdı
- `insider_purchases` etiketinin 0. kolondan **konumla** okunduğu; başlık
  `Insider Purchases Last 3m` olduğunda da çalıştığı; desen tutmazsa `failed`
- `insider_roster`'ın 7/9/11 kolonlu üç fixture'da da çalıştığı; kolon
  **sırası** değiştiğinde de doğru eşlediği
- `positionSummary` dolu satırın hisse bilgisini kaybetmediği
- `Position Indirect Date`'in hem `Timestamp` hem ham epoch `float` ile
  çözüldüğü
- **PFE'nin özdeş iki satırının tekilleştirildiği** ve `rows_attempted`'ın
  tekilleştirme sonrası sayıyı verdiği
- `'D/I'` değerinin kırpılmadan yazıldığı
- `pd.NA` taşıyan `Int64`/`Float64` serilerinin NULL'a çevrildiği; aynı
  alanın `int64` geldiği fixture'da da çalıştığı
- `0.0` / `0` değerlerinin **NULL'a çevrilmediği**
- `''` gelen `Position`/`ToGrade`/`Transaction`/`URL` alanlarının NULL olduğu
- `firm=''` gelen satırın yazılmayıp `failed` ürettiği
- `DECIMAL(38,10)` round-trip: `1.97656` ve `1285436390920` aynı kolonda
- `funds_data` ön kontrolünün fon olmayan sembolde **hiç istek yapmadığı**
  (çağrı sayacı ile) ve `KeyError` sızdırmadığı
- Tahvil fonunda `fund_weightings`'in yalnız `bond_rating` ürettiği
- `fund_top_holdings`'te evren dışı sembolün `is_known=0` ile yazıldığı
- `sustainability` dolu geldiğinde `WARNING` loglandığı ve hiçbir tabloya
  yazılmadığı

### 9.2 Repository testleri (gerçek MySQL, ağsız)

- Aynı payload iki kez → ikincisi **`skipped`**, veri tabloları değişmez
- **Kapı satırının `fetched_at`'i hash eşitken de güncellendiği**
- **`asof_state.first_seen_at`'in ikinci yazımda değişmediği**
  (`update_columns` kapsamı)
- **Hash'in `as_of_date`/`fetched_at`'ten bağımsız olduğu** — aynı veri farklı
  `fetched_at` ile aynı hash'i üretir. Bu iddia olmadan `VOLATILE_COLUMNS`
  bir gün sessizce daralır ve mekanizma hiç çalışmaz hâle gelir
- **Hash'in satır sırasından bağımsız olduğu** — aynı satırlar karıştırılınca
  hash değişmez (§6.1)
- **Boş sonuçta kapı satırının yazılmadığı**
- **Kaynak boşaldığında `replace_scope`'un eski satırları sildiği**
  (`scope_values` açıkça verildiği için)
- `scope_columns` ile silmenin komşu `holder_type`'a dokunmadığı
- İki dataset'in `institutional_holders`'da çakışmadan yaşadığı
- **`fund_metrics`'te aynı `metric` adının iki `section`'da yaşayabildiği**
- Ertesi gün farklı hash ile ikinci `as_of_date` satırı oluştuğu, öncekinin
  korunduğu
- `asof_state` yazımının veri yazımıyla aynı transaction'da olduğu
- `symbols`'tan silmenin `RESTRICT` ile engellendiği;
  `fund_top_holdings.holding_symbol`'ün engellemediği
- PK boyutlarının (1 055 / 548 / 296 byte) kabul edildiği
- `estimated_row_size(fund_profile)` < 65 535
- `sync_runs.selector`'ın migration sonrası NULL kaldığı
- **`holding_rank` kolonunun ham SQL'de backtick'siz de çalıştığı** (ad
  `rank` olsaydı `ERROR 1064`)

### 9.3 Canlı entegrasyon testi (`-m live`)

Varsayılan atlanır. 11 referans sembol × 17 dataset uçtan uca; ardından satır
sayıları, NOT NULL alanlar, FK bütünlüğü, `asof_state.row_count` tutarlılığı.

**İki iddia testle bağlanır:**

- **İstek sayısı:** `YfData.get_raw_json` sarmalanarak 17 dataset'in tek
  sembolde **7 istek** (fonlarda 8) yaptığı. Sayı sessizce artarsa (biri taze
  `Ticker` kurarsa) test kırılır — §4.4'ün varsayımını koda bağlayan tek şey.
- **404 kuralı:** `hide_exceptions=False` altında `^GSPC` için 16 hücrenin de
  `empty` olduğu (`failed` değil) ve run'ın çıkış kodu **0** verdiği.

Boş gelmesi beklenen hiçbir hücreye "dolu olmalı" iddiası kurulmaz.

### 9.4 Statik analiz

`ruff` + `mypy --strict`. `AsOfDataset[RawT]` `Dataset[RawT]` sözleşmesini
daraltmaz. `date_range` bir `Literal`'dir; eleme `match` ile yazıldığında
mypy eksik dalı yakalar. `RowWriter` protokolüne dokunulmadığı için mevcut
test sahteleri (`tests/unit/test_persistence_contract.py:21`,
`tests/unit/test_market.py:146`) **değişmeden geçer**.

---

## 10. Uygulama sırası ve migration

Tek Alembic revizyonu: 18 yeni tablo + `sync_runs.selector`.

1. `Dataset.date_range` + `SyncContext.start/end` (davranış değişmez)
2. `runner.py` 1–5 ve `shard.py` 7–8 (§6.5) + `sustainability`'siz denetim testi
3. `open_run(selector=...)` ve üç çağrı yeri (§6.5/6)
4. `models/analysis.py`, `models/holders.py`, `models/funds.py`,
   `models/asof.py` + migration
5. `datasets/asof_base.py` (`AsOfDataset`)
6. ~~`client.py`: `hide_exceptions=False` + 404 kuralı~~ — **zaten mevcut**
   (`client.py:149-153`, `errors.py:197-209`). Bu adımda yalnızca 17 yeni
   dataset'in `fetch`'inin `call_optional` kullandığı doğrulanır
7. Dokuz analist dataset'i (`datasets/analysis/`)
8. Altı sahiplik dataset'i (`datasets/holders/`)
9. `funds_data` (`datasets/funds.py`) — ön kontrol dâhil
10. `sustainability` izleme dataset'i
11. `cli.py`: beş seçenek + `symbols exchanges`
12. `"api"` seviyesinin mevcut beş dataset'e bağlanması
13. Fixture yakalama + testler

Adım 6 tek başına doğrulanabilir olmalıdır: `^GSPC` gibi bir sembolde 16
(`sustainability` varsayılan kapalı olduğu için)
hücrenin de `empty` (`failed` değil) olduğu ve çıkış kodunun 0 kaldığı.

### Proje yapısına eklenenler

```
src/yfin/
├── datasets/
│   ├── asof_base.py            # AsOfDataset
│   ├── analysis/
│   │   ├── base.py             # PeriodFrameDataset (0q/+1q/0y/+1y ortak tabani)
│   │   ├── recommendations.py  grade_changes.py  price_targets.py
│   │   ├── estimates.py        # earnings_estimate + revenue_estimate
│   │   ├── eps.py              # eps_trend + eps_revisions
│   │   ├── earnings_history.py growth.py  sustainability.py
│   ├── holders/
│   │   ├── breakdown.py        institutional.py   # institutional + mutualfund
│   │   ├── insider_activity.py insider_transactions.py  insider_roster.py
│   └── funds.py
├── models/{analysis,holders,funds,asof}.py
tests/fixtures/{PFE,XOM,NVDA,WMT,KO,BND,^GSPC}/
```

---

## 11. Yapılandırma ve kullanım

```
YF_PROBE_SUSTAINABILITY=0    # esgScores 19 sembolde de 404; varsayilan kapali
```

```bash
yfin db upgrade
yfin symbols exchanges                                    # evrendeki borsalar ve sayilari

yfin sync --datasets analysis                             # 9 dataset, sembol basina 6 istek
yfin sync --datasets holders                              # 6 dataset, sembol basina 1 istek
yfin sync --datasets funds                                # yalnizca ETF/fonlarda calisir

yfin sync --exchange IST --datasets analysis
yfin sync --exchange NMS,NAS,NYQ,PCX --datasets holders
yfin sync --quote-type EQUITY --datasets analysis         # kripto/FX/vadeli/endeks atlanir
yfin sync --suffix .IS --datasets holders                 # borsa cozulmeden de calisir

yfin sync --symbols AAPL --datasets upgrades_downgrades --start 2015-01-01 --end 2018-12-31
yfin sync --exchange IST --datasets history --start 2020-01-01   # gercek geriye donuk cekim
```

Yeni çalışma zamanı bağımlılığı **yoktur**.

---

## Ek A — Ölçüm günlüğü ve inceleme geçmişi

Bu doküman üç bağımsız incelemeden geçti (2026-09-04): mimari tutarlılık
(spec + `src/yfin/` kaynak okuması), MySQL şema doğrulaması (18 tablo gerçek
MySQL 8.3'te oluşturuldu, izole şemada denendi, silindi) ve yfinance API
doğrulaması (29 hisse + 10 fon + 4 egzotik sembol, canlı).

**Kapatılan kritik bulgular:**

| Bulgu | Nerede düzeltildi |
|---|---|
| `--start/--end` shard sınırını geçmiyordu; proxy havuzu doluyken aralık sessizce yok sayılırdı | §6.5/7-8 — `ShardSpec` alanları |
| `_failed_records` de `produces=()` körü; `sustainability` **hata** durumunda da denetimden kaybolurdu | §6.5/2 |
| Aralık elemesi için `SymbolPayload`'da kanal yoktu | §6.5/3-5 |
| `hide_exceptions=False` her egzotik sembolde 16 `failed` üretirdi; §4.1/§4.3/§8.2/§6.3'ü birden çürütüyordu | §8.4 — HTTP 404 → `empty` |
| `funds_data` `hide_exceptions=False` altında `YFDataException` değil ham `KeyError` fırlatıyor | §6.3, §8.3 |
| `fact_hash` `insider_transactions`'ı tekilleştirmiyor (PFE'de dokuz kolonda özdeş satır) | §5.2, §8.3 — birebir tekilleştirme |
| `Ownership = 'D/I'` `VARCHAR(2)`'yi kırıyordu | §5.2 — `AsciiKeyType(8)` |
| `insider_roster` 11 kolona çıkıyor; `positionSummary` tek hisse bilgisi olabiliyor | §5.2 — iki yeni kolon |
| `fast_info['quoteType']` "SPY'da `None`" iddiası **ölçüm hatasıydı** (`quote_type` snake_case okunmuş); `'ETF'` dönüyor | §4.2, §6.3 — `depends_on` sadeleşti |
| `fund_metrics` PK'sı `section`'ı dışlıyordu → `ERROR 1062` | §5.3 |
| Kapı satırı hash eşitken yazılmıyordu; `fetched_at` "son doğrulama" olamıyordu | §6.1/4 — `hash_gated` ilkesi |
| `first_seen_at` `ON DUPLICATE KEY UPDATE` ile ezilirdi | §6.1/5 — `update_columns` |
| Boş sonuçta kapı satırı yazılıp `first_seen_at` semantiği bozulurdu | §6.1/3 |
| Hash satır **sırasına** duyarlıydı | §6.1 — satırlar `key_columns`'a göre sıralanır |
| `asof_state` hiçbir `produces`'ta yoktu (`test_registry` ihlali) | §6.1 |
| `replace_scope` + boş liste → eski satırlar kalıcı olurdu | §7.2 — `scope_values` açıkça |
| `rank` MySQL 8'de ayrılmış sözcük | §5.3 — `holding_rank` |
| `date_reported`/`net_trans` ölçümsüz NOT NULL / UNSIGNED'dı | §5.2 |
| İki indeks PK önekiydi, `EXPLAIN` gereksizliğini gösterdi | §5.8 |
| `RowWriter`'a metot eklemek test sahtelerini kırardı | §0, §6.1 — protokol değişmiyor |
| 9 tabloda nullability tablo hâlinde verilmemişti (T§5.2 standardı) | §5.1–5.4 |
| `insider_transactions` penceresi "~24 ay" değil **150 satır tavanı** | §4.5, §7.3 |
| `upgrades_downgrades` güncel olmayabiliyor (META 2024-09-30) | §4.5 |
| `AsOfDataset`'in mevcut iki hash kapısıyla ilişkisi tanımsızdı | §6.1 — karşılaştırma tablosu |
| `unknown_symbol` + as-of etkileşimi yazılmamıştı | §8.1 |
| Çok tablolu as-of'ta `empty`/`skipped` karışımı tanımsızdı | §7.2 |
| Ölçüm düzeltmeleri: `position` 23→56, `insider` 26→33, `Holder` 69→70, `Value` 3,82×10¹¹→1,76×10¹³ | §4.1, §4.2 |
