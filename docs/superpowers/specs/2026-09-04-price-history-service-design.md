# yfinance Price History Servisi → MySQL — Tasarım Dokümanı

**Tarih:** 2026-09-04
**Durum:** onaylandı, uygulanmayı bekliyor
**Kapsam:** `price_history.html` referansındaki çoklu interval fiyat serisi;
`price_bars`, `intraday_scope`, `bar_gaps`, `bar_rescales` tabloları
**Kısaltmalar:** Bu dokümanın kendi bölümleri metinde `§X.Y`, ASCII
docstring'lerde `PB§X.Y` (price-bars) olarak anılır. Diğer dokümanlar:
`S` = ETL, `F` = financials/market, `AH` = analysis/holdings, `P` = proxy.

**Önceki dokümanlar:** `2026-09-04-yfinance-mysql-etl-design.md` (S),
`2026-09-04-yfinance-financials-market-design.md` (F),
`2026-09-04-yfinance-analysis-holdings-design.md` (AH),
`2026-09-04-proxy-pool-and-yfinance-advanced-design.md` (P)

Bu dokümandaki her sayısal iddia ölçülmüştür. Ölçüm komutları ve ham
çıktıları Ek A'dadır. Ölçülmemiş hiçbir varsayım karar gerekçesi olarak
kullanılmamıştır; ölçüm sırasında ÇÜRÜTÜLEN varsayımlar §4.2'de açıkça
listelenmiştir.

---

## 0. Önkoşullar

Bu tasarım mevcut hattın üzerine kurulur ve şunları VERİLİ kabul eder:

- `Dataset` sözleşmesi (`datasets/base.py`): `fetch` → `normalize` →
  `upsert`, `TableWrite`, `NormalizedResult`, `SyncContext`.
- `Registry` (`datasets/registry.py`): alias genişletme, topolojik sıralama,
  `bootstrap="symbols"`.
- Shard'lı koşu ve proxy havuzu (P §4): koordinatör + child process'ler,
  shard başına tz/cookie cache izolasyonu.
- `RowWriter` protokolü (`persistence.py`): MySQL mekaniği dataset'lerden
  ayrıdır.
- `symbols` tablosu ve `symbol_fk_column` (S §5.5).
- `price_history` tablosu (S §5.2, P §3.7): `interval='1d'`,
  `auto_adjust=False`, `repair=True`, `is_repaired` monotonik kolonu.

**Bu tasarım `price_history` tablosuna ve `history` dataset'inin
fetch/normalize/upsert davranışına DOKUNMAZ.** Gerekçe §2'de (K1).

Tam sınır: `history`'nin kendi çıktısı, testleri ve `FRAME_CONSUMERS`
watermark mantığı değişmez. Buna karşılık `rescale` kancası (§6.6)
`splits` **tablosunu** okur ve sembolün yazma transaction'ına bir adım
ekler — yani `history`'nin ÜRETTİĞİ VERİYE dokunmadan, koşu akışına
dokunur. Ayrıca `persistence.py` (§6.7), `runner.py` (§6.3, §6.5) ve
`ItemStatus` (§6.5c) tüm dataset'ler için ortak olan noktalardan
değişir; bunlar geriye uyumludur ama "hiçbir şey değişmiyor" demek
yanlış olurdu.

---

## 1. Amaç

`price_history` bugün yalnızca `interval='1d'` yazıyor. Bu doküman, aynı
sembol evreni için intraday ve çok-günlük barları KALICI BİR ARŞİV olarak
MySQL'e yazan servisi tanımlar.

Kalıcılık burada süsleme değil, tasarımın ekseni: **Yahoo `1m` verisini
yalnızca son 30 gün, diğer dakikalık intervalleri 59 gün geriye veriyor
(ölçüldü, §4.3). Bir kez kaçırılan intraday bar BİR DAHA ÇEKİLEMEZ.**
Bu tek gerçek, aşağıdaki kararların çoğunu tek başına belirliyor: neden
silme yok, neden boşluklar tabloya yazılıyor, neden split'te geçmişi
kendimiz yeniden ölçeklemek zorundayız.

### Kapsam içi

- Altı interval: `1m`, `5m`, `15m`, `60m`, `1wk`, `1mo` → `price_bars`.
- `prepost=True`: seans dışı barlar yazılır, `is_extended` ile işaretlenir.
- `1m` için sembol alt kümesi (`intraday_scope` tablosu).
- Pencere planlayıcı: Yahoo'nun istek başına sınırlarına göre dilimleme.
- Kaçırılan pencerelerin kalıcı kaydı (`bar_gaps`).
- Split'te geriye dönük yeniden ölçekleme (`bar_rescales`).
- Aylık bakım işi: partition ekleme, öksüz satır denetimi, boşluk raporu.
- MySQL `RANGE COLUMNS(ts_utc)` partitioning.

### Kapsam dışı — gerekçeli, tahminle değil ölçümle

| Dışlanan | Gerekçe (ölçüm) |
|---|---|
| `yf.download()` | İstek AZALTMAZ: sembol başına ayrı `Ticker.history()` çağırır (`multi.py:165-172, 284`). `multitasking` ile kendi thread havuzunu açıp `call_yahoo` rate limiter'ını, retry'ı ve shard proxy'sini bypass eder. `reindex_dfs` çok pazarlı evrende uydurma satır üretir: `["AAPL","THYAO.IS"]` 1h → AAPL'de 126, THYAO'da 90 NaN hücre. `hide_exceptions`'ı global değiştirip geri yazar (`multi.py:280-281, 304`) — thread'ler arası yarış. **Not:** ölçümde bu mutasyonun `YfConfig.**network**.hide_exceptions`'a yapıldığı, oysa yfinance'in tamamının (ve projenin `client.py:152`'sinin) `YfConfig.**debug**.hide_exceptions`'ı okuduğu görüldü — yani yarış bugün projenin ayarını etkilemiyor; dışlama diğer üç gerekçeyle ayakta. §4.2 |
| `2m` | `1m` arşivi varken kayıpsız türetilebilir. Bedeli yılda ~605 milyon satır ve sembol başına günlük 1 istek. |
| `30m` | Yahoo'dan gelmiyor: yfinance `15m` çekip resample ediyor (istek tarafı `history.py:261-262`, resample `history.py:405-408` `quotes.resample('30min')`, log metni `:355`). `15m` saklanıyorken bu, aynı veriyi ikinci kez yazmaktır. |
| `90m` | Yahoo'ya özgü, standart dışı; `60m` ve `15m` varken bilgi katkısı yok. |
| `1h` | `60m` ile aynı: ikisi de `history.py:236` içinde aynı 730 günlük dala düşer. |
| `5d` | Bozuk. 3 aylık istek 13 satır döndürdü, index `06-04, 06-09, 06-24` — düzensiz. yfinance kaynağı: *"Yahoo's interval '5d' is nonsense"* (`history.py:170`). |
| `3mo` | `repair=True` yolunda çeyrek hizalaması `datetime.now().strftime('%b')`'e bağlı (`history.py:774-777`) → ay değiştikçe bar sınırları kayar, satır anahtarları kararsız. |
| `back_adjust=True` | `Adj Close` kolonunu SİLİYOR ve OHLC'yi değiştiriyor (ölçüldü). Bilgi kaybı. |
| `rounding=True` | `319.55999755859375` → `319.56`. Ham veriyi kırpar. |
| `raise_errors` | yfinance 1.7.0'da DEPRECATED (`history.py:155`); yerine `yf.config.debug.hide_exceptions` — P §6.2'de zaten yapılandırılıyor. |
| `repair=True` (price_bars'ta) | İstek maliyeti: `5m` 1→6, `15m` 1→8 (§4.4), yani sembol başına +12 ek istek; 5.000 sembolde **+60.000 ek istek/gün** (§7.4). Ayrıca `1wk`/`1mo`'yu `1d`'den resample edip satır anahtarlarını kaydırır. `1d`'de AÇIK KALIR (mevcut davranış). |
| `adj_close` (price_bars'ta) | Çekim anındaki duruma göredir; kalıcı arşivde bayatlar (§5.1). |
| Çok kaynaklı şema (`source` kolonu) | Proje Yahoo Finance özelindedir. İlişkiler `symbols.symbol` üzerinden kurulur. |
| Intraday budama/retention | Karar: kalıcı arşiv, silme yok. `prune.py`'nin kapsamına GİRMEZ. |

---

## 2. Kararlar ve gerekçeleri

| # | Karar | Gerekçe |
|---|---|---|
| K1 | `price_history` (1d) korunur; yeni `price_bars` tablosu açılır | Anahtar semantiği FARKLI: `price_history`'nin otoritesi borsanın yerel seans tarihi (S §5.4: "pozitif ofsetli borsalarda UTC'ye çevirmek tarihi bir gün geri kaydırır"), `price_bars`'ınki mutlak zaman damgası. `GC=F` ölçümü tuzağı gösteriyor: bar 18:10'da açılıyor, seans günü ≠ takvim günü. Yan fayda: olgun 1d hattı, `v_actions`, `FRAME_CONSUMERS` watermark mantığı ve `is_repaired` monotonluğu değişmez. |
| K2 | `PARTITION BY RANGE COLUMNS(ts_utc)` (`MAXVALUE` YOK), FK YOK | MySQL 8: partition'lı InnoDB tablosu foreign key desteklemez. 464 milyon satırda partition pruning ve sığ indeks ağacı, FK'nın DB-seviyesi garantisinden daha değerli. Bütünlük yazım yolunda (`symbols` bootstrap'ı önce koşar) + aylık öksüz satır sorgusuyla korunur (§7.5/2). PostgreSQL/TimescaleDB'ye geçişte FK GERİ GELİR (§12). `MAXVALUE` bölümü ölçümle elendi (§5.3). |
| K3 | `adj_close`, `dividend`, `split_ratio`, `capital_gain` kolonları YOK | İlk ikisi bayatlar/türevdir; son üçü zaten `price_history`'de bile "TÜREV bilgidir, otorite değildir" notuyla duruyor. 3×14 B × 464 milyon ≈ 19,5 GB saf tekrar. Otorite `dividends`/`splits`/`capital_gains` tablolarıdır. |
| K4 | Split'te geriye dönük yeniden ölçekleme + kurulum tohumu | Yahoo HAM FİYAT VERMİYOR: `auto_adjust=False` olmasına rağmen OHLC split-adjusted geliyor (NVDA 2024-05-20 `Open=93.75`, gerçekte ~937.50). Split olduğunda Yahoo tüm geçmişi ölçekler; arşiv ölçekleyemez → karışık ölçekli tablo ve sahte 10× sıçrama. Mekanizma `rescale --seed` OLMADAN arşivi kendisi bozar (§6.6). |
| K5 | `interval` tipi `VARCHAR(4) ascii_bin`, ENUM DEĞİL | ENUM'a değer eklemek `ALTER TABLE`'dır; 464 milyon satırlı tabloda çok uzun sürer. Geçerli küme Python tarafında `BAR_INTERVALS` sabitinde — projenin "alan tanımlarının tek kaynağı" deseni (S §6.6). |
| K6 | `1m` yalnız `intraday_scope`'taki sembollere | Tüm evren için 1,21 milyar satır/yıl (~129 GB). 500 sembollük liste konfigürasyon değil VERİDİR; `.env`'de tutulamaz. |
| K7 | Kalıcı arşiv, silme yok; aylık BAKIM işi | Veri geri getirilemez. Bakım = partition ekleme + denetim + rapor, silme değil. |
| K8 | `repair` yalnız `1d`'de | İstek maliyeti 6-8× (§4.4) ve `1wk`/`1mo` anahtar kayması. Sonuç: `price_bars`'ta `is_repaired` kolonu YOK. |
| K9 | Tek parametrik `IntervalBarDataset`, altı örnek | Altı sınıf = aynı gövdenin altı kopyası. |
| K10 | `bar_gaps` tablosu + `resolved_at` | Kalıcı arşivde "veri yok" iki farklı şey olabilir: piyasa kapalıydı, ya da biz kaçırdık. Bu ayrım SONRADAN yapılamaz. `resolved_at` sayesinde tablo bir mezar taşı değil, planlayıcının yeniden denediği bir GÖREV LİSTESİDİR (§6.2/5, §8.4). |
| K11 | `INSERT_CHUNK` yazma katmanına eklenir (modül sabiti) | Kilit süresi ve ya-hep-ya-hiç geri alma; `max_allowed_packet` gerekçesi ÖLÇÜMLE ELENDİ (20.000 satır ≈ 3-4 MB, sınır 64 MB). §6.7 |
| K12 | MySQL'de kurulur, PostgreSQL+TimescaleDB geçişi hazırlanır | Mevcut 40+ tablo, migration zinciri, testler ve shard mimarisi tek parça kalır; `symbols` ile JOIN çalışır. MySQL'e özgü her karar §12'de işaretli. |

---

## 3. Ortam

- Python 3.13, `yfinance==1.7.0`, `yfinance[repair]` ekstrası kurulu
- MySQL 8.x, InnoDB, `utf8mb4_0900_ai_ci` (istisnalar S §5.1)
- SQLAlchemy 2.x, Alembic, PyMySQL
- Ölçümler 2026-09-04'te, doğrudan bağlantıyla (proxy'siz) yapıldı
- Ölçüm sembolleri: `AAPL`, `MSFT`, `NVDA`, `SPY`, `THYAO.IS`, `SHEL.L`,
  `BTC-USD`, `EURUSD=X`, `GC=F`, `VWCE.DE`

---

## 4. API keşif bulguları

### 4.1 Doğrulanmış davranışlar

1. **Intraday index yerel borsa saatindedir ve tz taşır.** `AAPL` 1m →
   `dtype='datetime64[s, America/New_York]'`. `set_df_tz` +
   `fix_Yahoo_dst_issue` (`history.py:423-424`) uygulanmış hâlde gelir.
2. **Bar damgası bar BAŞLANGICIDIR.** `AAPL` 5m: ilk bar `09:30`, son bar
   `15:55` (seans 16:00'da kapanır).
3. **`Adj Close` intraday'de de gelir** (`auto_adjust=False` ile). 5m
   çıktısı 8 kolon: `Open, High, Low, Close, Adj Close, Volume, Dividends,
   Stock Splits`. ETF'lerde 9. kolon `Capital Gains` eklenir (`SPY`,
   `VWCE.DE` ölçüldü).
4. **`repair=True` intraday'de çalışır** ve `Repaired?` kolonu ekler —
   ama pahalıdır (§4.4).
5. **Örtüşen pencereler idempotenttir.** `-4g..-2g` ve `-3g..bugün` 5m
   çekimlerinde 78 ortak bar; `Open/Close/Volume` birebir eşit. Aynı istek
   iki kez → `df.equals()` True.
6. **`tradingPeriods` her fetch'te o fetch'in günlerini kapsar.** 5 günlük
   çekimde 4-6 satırlık DataFrame; kolonlar `pre_start, pre_end, start,
   end, post_start, post_end`. Normalize anında elimizdedir.
7. **`get_history_metadata()` EK AĞ İSTEĞİ DEĞİLDİR.** `history()` çağrısı
   metadata'yı doldurur; `history_metadata` dataset'i de aynı önbelleği
   paylaşır (`ctx.cached`).
8. **`has_pre_post_market_data` alanı zaten şemamızda var**
   (`models/fields.py:298`, `HISTORY_METADATA_FIELDS`).
9. **Yahoo'nun pencere reddi, BU PROJENİN yapılandırmasında EXCEPTION
   FIRLATIR.** `client.py:152` `yf.config.debug.hide_exceptions = False`
   ayarlıyor (P §6.2); `history.py:366-374` bu durumda
   `YFPricesMissingError` fırlatır. Ölçüldü:

   | `hide_exceptions` | retention aşımı (`1m`, -45g..-38g) |
   |---|---|
   | `True` (yfinance varsayılanı) | boş DataFrame, hata yalnız log'a |
   | **`False` (bu projenin ayarı)** | **`YFPricesMissingError` fırlar** |

   Bu, `empty` ≠ `failed` ayrımının temelidir ve §8.2'yi belirler. Aynı
   istisna tipi hem "pencere dışı" hem "veride bar yok" için kullanıldığı
   için ayrım MESAJ METNİNDEN yapılmak zorundadır — AH §8.4'ün
   "404 → empty" kuralının aynısı.

### 4.2 Çürütülen varsayımlar

| Varsayım | Gerçek |
|---|---|
| `yf.download()` çok sembollü tek istek atar, istek sayısını düşürür | Sembol başına ayrı `Ticker.history()`; istek sayısı AYNI. Yalnız `multitasking` ile paralelleştirir — bizim rate limiter, retry ve proxy katmanımızı bypass ederek. |
| `download()` hata izolasyonunu korur | `_download_one` exception'ı yutup `empty_df()` koyar (`multi.py:298-302`); `empty` ile `failed` ayırt edilemez hale gelir. |
| `download()` çıktısı sembol başına bağımsızdır | `reindex_dfs` tüm sembolleri ortak index'e hizalar ve "en yaygın tz"e çevirir. Ölçüldü: 1h × `["AAPL","THYAO.IS"]` → AAPL'de 126, THYAO'da 90 NaN hücre; THYAO barları `America/New_York`'a çevrilmiş (`2026-09-01 02:30-04:00`). 1d'de ayrıca `ignore_tz` varsayılanı tz'yi tamamen düşürür. |
| `period='max'` intraday'de tüm geçmişi verir | `1m` için yalnız **8 gün** (`history.py:232`), `2m-90m` için 60, `1h/60m` için 730. `period='max'` intraday'de "max" değildir. |
| `auto_adjust=False` ham (düzeltilmemiş) fiyat verir | Yalnız TEMETTÜ düzeltmesini ayırır. SPLIT düzeltmesi OHLC'ye uygulanmış gelir — NVDA 2024-05-20 `Open=93.75` (gerçek ~937.50), `Volume=528.402.000` (gerçek ~52,8 milyon). |
| `30m` Yahoo'dan gelen bir interval'dir | `history.py:262`: `if params["interval"] == "30m": params["interval"] = "15m"` — sonra resample. |
| `1h` ve `60m` farklı | Aynı dal (`history.py:234`). |
| `repair=True` yalnız CPU maliyetidir | AĞ maliyetidir: `_reconstruct_intervals_batch` daha ince interval çeker. `5m` 1→6 istek, `15m` 1→8. |
| `repair=True` interval semantiğini korur | `1wk`/`1mo`/`3mo`'yu `1d`'den resample eder (`history.py:166-192`). `1mo` ölçümü: repair'siz 24 satır/`2024-10-01`, repair'li 25 satır/`2024-09-01`. FARKLI SATIR ANAHTARLARI. |
| `keepna=True` eksik barları görünür kılar | Ölçülen örnekte fark üretmedi (312 satır, 0 NaN close). Boşluk tespiti için GÜVENİLEMEZ; `bar_gaps` planlayıcıdan beslenir (§6.2). |

### 4.3 Pencere sınırları matrisi (ölçüldü)

| interval | istek başına azami | geriye azami derinlik | ilk dolum isteği |
|---|---|---|---|
| `1m` | **8 gün** (9 → *"Only 8 days worth of 1m granularity data are allowed to be fetched per request"*) | **29 gün** (30 → RED; 29 → 1950 bar) | 4 |
| `5m` | 59 gün (60 tam gün reddedildi) | 59 gün | 1 |
| `15m` | 59 gün | 59 gün | 1 |
| `60m` | **729 gün** (730 reddedildi) | 729 gün | 1 |
| `1wk` | sınırsız | ~99 yıl (`history.py:238`) | 1 (`period="max"`) |
| `1mo` | sınırsız | ~99 yıl | 1 (`period="max"`) |

**Tablodaki değerler ÖLÇÜMDE KABUL EDİLEN azami değerlerdir, Yahoo'nun
ilan ettiği sınır değil.** Yahoo'nun mesajları "30 gün" / "60 gün" /
"730 gün" der; ölçümde 30, 60 ve 730 REDDEDİLDİ, 29, 59 ve 729 kabul
edildi.

`1m` derinliği ilk yazımda 30 bırakılmıştı — tam sınır ölçülmemişti
(`-25g..-18g` ölçülmüştü, o çalışıyor). **Hata ilk canlı koşuda
yakalandı:** planlayıcı ilk dilimi tam sınırda başlattı,
`YFPricesMissingError` geldi ve AAPL'in tüm `1m` ilk dolumu düştü. Bu,
§8.2'nin son cümlesinin ("planlayıcı doğru çalışıyorsa pencere reddi
HİÇ görülmemelidir; görülmesi `BAR_LIMITS`'in saptığının erken
uyarısıdır") ilk kez ve beklenen şekilde işlemesidir. Sınırlar gün değil
SANİYE bazlıdır ve "şu ana" göreli olduğu için tam sınır her zaman
risklidir. `BAR_LIMITS` bu kabul edilmiş değerleri taşır; planlayıcının
ayrıca tampon uygulamasına gerek YOKTUR (§6.2).

Dilimleme YALNIZ `1m`'de gerekir — tek interval'de istek penceresi (8)
derinlikten (30) küçüktür.

### 4.4 İstek maliyeti — `repair` (ölçüldü)

`YfData.get` ve `YfData.cache_get` hook'lanarak, önbellek boşken sayıldı:

| interval | `repair=False` | `repair=True` | çekilen alt interval'ler |
|---|---|---|---|
| `5m` | 1 | **6** | `2m`, `1m` |
| `15m` | 1 | **8** | `5m`, `2m` |
| `60m` | 1 | 2 | — |
| `1d` | 1 | 2 | — |
| `1wk` | 1–3 | 1 | `1d` (resample) |
| `1mo` | 1–2 | 2 | `1d` (resample) |

**`1wk`/`1mo`'nun istek sayısı KARARSIZDIR.** Farklı sembollerde
tekrarlandı: `KO 1wk` → 1 istek, `PEP 1wk` → 3 istek (biri `1d`).
Fark, sembolün tz/metadata önbelleğinin sıcak olup olmamasından ve
`actions=True`'nun temettü/split için tetiklediği `_get_history_cache`
çağrısından geliyor. Bütçe bu yüzden sabit bir sayıya değil, ÜST SINIRA
dayandırılır (§7.4).

`_reconstruct_intervals_batch` `1m`'de erken döner (`history.py:815-817`:
*"Can't go smaller than 1m so can't reconstruct"*), diğerlerinde ince
interval çeker. Zincir `["1wk","1d","1h","30m","15m","5m","2m","1m"]`
üzerinde en fazla 2 derinlik iner (`history.py:834-853`): `5m→2m→1m`,
`15m→5m→2m`.

**Bu sayılar SABİT DEĞİL, GÖZLENEN EN KÖTÜ DURUMDUR.** İstek, onarım
gerektiren her bitişik "bozuk blok" için ayrı açılır; alt interval'in
kendi penceresinden eski bloklar istek yapılmadan atlanır
(`history.py:952-954`). Temiz bir çerçevede maliyet 1'dir. K8'in
gerekçesi bu belirsizliğin kendisidir: maliyeti önceden bilinemeyen bir
özellik, 5.000 sembollük bir bütçenin içine konamaz.

### 4.5 Çok pazarlılık matrisi (5m, 5 gün, `prepost=True`)

| sembol | tz | satır | ilk bar | `has_pre_post` | kolon |
|---|---|---|---|---|---|
| `AAPL` | America/New_York | 312 → 768 (prepost) | 09:30 → 04:00 | **True** | 8 |
| `SPY` | America/New_York | 312 | 09:30 | True | 9 (Capital Gains) |
| `THYAO.IS` | Europe/Istanbul | 388 → 388 (+0) | 09:55 | False | 8 |
| `SHEL.L` | Europe/London | 306 → 311 (**+5**) | 08:00 | False | 8 |
| `VWCE.DE` | Europe/Berlin | 407 → 415 (**+8**) | 09:00 | False | 9 |
| `BTC-USD` | UTC | 1433 | 00:00 (7/24) | False | 8 |
| `EURUSD=X` | Europe/London | 1152 | 00:00 | False | 8 |
| `GC=F` | America/New_York | 1175 | **18:10** | False | 8 |

Dört ders:

1. **`has_pre_post_market_data` GÜVENİLMEZ BİR KAPI DEĞİLDİR — kapı
   olarak KULLANILAMAZ.** İlk okumada "yalnız ABD hisselerinde ek bar
   gelir" sanılmıştı; iki karşı örnek bunu çürüttü. `SHEL.L` ve
   `VWCE.DE` alanı `False` bildirdiği hâlde `prepost=True` ile sırasıyla
   5 ve 8 ek bar döndürüyor (`SHEL.L`: 16:30, 16:35 — `end=16:30`'un
   sonrası). Bu alana dayanan bir `is_extended` kuralı o barları normal
   seans sayar ve `v_price_bars_regular`'a sokar; yani view'ın önlemek
   için var olduğu bozulmanın ta kendisini üretir. **Tek doğruluk
   kaynağı `tradingPeriods`'ın `start`/`end` aralığıdır** (§6.4).
2. **`tradingPeriods`'ın dejenere olan kolonları `pre_*`/`post_*`'tur,
   `start`/`end` DEĞİL.** İlk ölçüm yanlış okunmuştu. Gerçek değerler:
   `THYAO.IS` `start=09:30, end=18:00` (`pre_start==pre_end==09:30`,
   `post_start==post_end==18:00`), `SHEL.L` `start=08:00, end=16:30`,
   `BTC-USD`/`GC=F` `start=00:00, end=23:59`. `start`/`end` her sembolde
   ANLAMLIDIR ve `is_extended` yalnız ona dayanır.
3. **`tradingPeriods`'ın KOLON SETİ `prepost` parametresine bağlıdır.**
   `prepost=False` ile çekildiğinde çerçeve yalnız `start`/`end`
   taşır; `pre_start` erişimi `KeyError` verir. `price_bars` daima
   `prepost=True` çektiği için altı kolon da gelir, ama normalize
   koduna güvenli erişim (`.get`/kolon varlık kontrolü) yazılmalıdır.
4. **`GC=F` barı 18:10'da açılıyor**: seans önceki takvim gününde başlayıp
   ertesine sarkıyor. `local_date` bu yüzden "seans günü" DEĞİL, "barın
   yerel takvim tarihi" olarak tanımlanmıştır (§5.1).

### 4.6 Split ölçeklemesi (ölçüldü) — K4'ün kanıtı

NVDA, 2024-06-10, 10:1 split. `auto_adjust=False, repair=False`:

```
2024-06-05  Close=122.44  Volume=528.402.000  Stock Splits=0
2024-06-07  Close=120.89  Volume=412.386.000  Stock Splits=0
2024-06-10  Close=121.79  Volume=313.434.100  Stock Splits=10.0
```

Split ÖNCESİ günlerde fiyat ~120 (gerçekte ~1.200), hacim ~5×10⁸
(gerçekte ~5×10⁷). Yani Yahoo geçmişi split'e göre yeniden ölçeklemiş:
**fiyat bölünmüş, hacim çarpılmış.**

`price_history` bunu umursamaz — her koşuda tüm geçmişi yeniden yazar.
`price_bars` yazamaz: 30 günü geçmiş `1m` barı yeniden çekilemez. Ölçeği
korumak bize düşer (§6.6).

### 4.7 Çok-günlük bar damgaları (ölçüldü)

| interval | damga | örnek |
|---|---|---|
| `1wk` | haftanın Pazartesi'si, yerel 00:00 | `2025-09-01 00:00-04:00` |
| `1mo` | ayın 1'i, yerel 00:00 | `2025-10-01 00:00-04:00` |

DST nedeniyle offset değişir: `2026-03-02 00:00-05:00` ve
`2026-03-09 00:00-04:00`. `ts_utc` ile saklamak bu yüzden tutarlıdır;
"hangi hafta" sorusu `local_date`'ten cevaplanır.

---

## 5. Veri modeli

Dört yeni tablo. Hiçbiri mevcut tabloları değiştirmez; tek istisna
`sync_runs`/`sync_run_items`'ın yeni dataset adlarını görmesi (§5.9).

### 5.1 `price_bars`

```python
class PriceBar(Base):
    """price_history'nin intraday/cok-gunluk kardesi.

    ANAHTAR MUTLAK ZAMAN DAMGASIDIR, seans tarihi degil (K1). price_history
    yerel seans tarihiyle anahtarlanir cunku gunluk barin otoritesi
    borsanin takvimidir; intraday barin otoritesi ise mutlak zamandir.
    GC=F olcumu farki gosterir: bar 18:10'da acilir, yani barin yerel
    takvim gunu seans gunu DEGILDIR (PB§4.5).

    FK YOKTUR (K2): MySQL 8 partition'li InnoDB tablosunda foreign key
    desteklemez. `symbol` yine symbols.symbol'a isaret eder; butunluk
    yazim yolunda (symbols bootstrap'i once kosar) ve aylik oksuz
    satir sorgusuyla korunur (PB§7.5/2, PB§8.7).
    """

    __tablename__ = "price_bars"
    __table_args__ = (
        Index("ix_price_bars_local_date", "local_date"),
        MYSQL_TABLE_ARGS,
    )

    # symbol_fk_column DEGIL: FK tasimaz, ama TIPI birebir aynidir.
    # Ayni SymbolType() kullanilir; farkli genislik/collation ileride
    # symbols ile JOIN'de ERROR 3780 uretirdi (S5.1).
    symbol: Mapped[str] = mapped_column(SymbolType(), primary_key=True)
    bar_interval: Mapped[str] = mapped_column(BarIntervalType(), primary_key=True)
    ts_utc: Mapped[datetime] = mapped_column(TsType(), primary_key=True)

    # TUREV kolon: barin YEREL TAKVIM TARIHI. Adi bilerek session_date
    # DEGILDIR - price_history.session_date bir SEANS gunudur, bu ise
    # yalnizca yerel takvim gunu. Ayni adi tasisalardi iki farkli kavram
    # karisirdi (GC=F: 18:10 bari, seans ertesi gune ait).
    local_date: Mapped[date] = mapped_column(nullable=False)

    open: Mapped[Decimal | None] = mapped_column(PriceType())
    high: Mapped[Decimal | None] = mapped_column(PriceType())
    low: Mapped[Decimal | None] = mapped_column(PriceType())
    close: Mapped[Decimal] = mapped_column(PriceType(), nullable=False)
    volume: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))

    # Seans disi (pre/post market) bari mi. has_pre_post_market_data=False
    # olan sembollerde her zaman 0'dir (PB§6.4).
    is_extended: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="0"
    )
```

**Bilinçli olarak YOK olan kolonlar** (K3), her biri ayrı gerekçeyle:

- **`adj_close`**: Yahoo'nun `Adj Close`'u çekim anındaki split/temettü
  durumuna göredir. `price_history` her koşuda tüm geçmişi yeniden yazdığı
  için orada hep tazedir; `price_bars` kalıcı arşivdir — 2027'de yazılan
  bir bar 2030'daki temettü serisinden habersiz kalır ve tablo karışık
  düzeltme durumları taşır. Ham OHLC saklanır; temettü düzeltmesi sorgu
  zamanında `dividends`'tan hesaplanır. Bu zaten projenin ilkesi
  (`models/prices.py`: "tekil doğruluk kaynağı dividends/splits/
  capital_gains tablolarıdır"). Kazanç: 14 B/satır × 464 milyon
  ≈ **6,5 GB/yıl**.
- **`dividend` / `split_ratio` / `capital_gain`**: `price_history`'de bile
  "TÜREV bilgidir, otorite değildir" notuyla duruyorlar. 3 × 13 B ×
  464 milyon ≈ **19,5 GB** saf tekrar. `v_actions` zaten yalnız otorite
  tabloları okuyor.
- **`is_repaired`**: `repair` `price_bars`'ta kapalıdır (K8), kolon
  daima 0 olurdu.
- **`ts_local` (offset'li kopya)**: `ts_utc` + `symbols`/`history_metadata`
  tz'si yeterlidir; ikinci bir zaman kolonu iki doğruluk kaynağı yaratırdı.

**Satır boyutu bütçesi** (S §5.4 yöntemiyle):

| bileşen | byte |
|---|---|
| `symbol` (VARCHAR ascii, ort. 6 karakter + 1 uzunluk) | ~7 |
| `bar_interval` (VARCHAR(4) ascii) | ~4 |
| `ts_utc` DATETIME(6) | 8 |
| `local_date` DATE | 3 |
| 4 × DECIMAL(28,12) | 56 |
| `volume` BIGINT UNSIGNED | 8 |
| `is_extended` | 1 |
| InnoDB satır başlığı + PK indeksi | ~20 |
| **toplam** | **~107 B/satır** |

`DECIMAL(28,12)` **14 bayttır**, 13 değil: MySQL her 9 ondalık haneyi 4
bayta paketler; 16 tam hane → 4+4 = 8 B, 12 kesir hanesi → 4+2 = 6 B.
(`models/kinds.py`'deki `"dec"` satır maliyeti 13 olarak yazılmış; o da
aynı düzeltmeyi hak ediyor ama bu tasarımın kapsamı dışında.)

### 5.2 Hacim projeksiyonu

Ölçülen bar yoğunluğu: `AAPL` 1m prepost'suz 390 bar/seans, prepost'lu
~960 bar/seans (04:00–19:55). 252 seans/yıl.

| interval | sembol | satır/yıl | ~boyut/yıl |
|---|---|---|---|
| `1m` | 500 (`intraday_scope`) | ~121 milyon | ~12 GB |
| `5m` | 5.000 | ~242 milyon | ~25 GB |
| `15m` | 5.000 | ~81 milyon | ~8 GB |
| `60m` | 5.000 | ~20 milyon | ~2 GB |
| `1wk` + `1mo` | 5.000 | ~320 bin | ~30 MB |
| **toplam** | | **~464 milyon** | **~50 GB** |
| + `ix_price_bars_local_date` | | | **~14 GB** |
| **birinci yıl sonu** | | **~464 milyon** | **~64 GB** |

**Bu tablo MUHAFAZAKÂR BİR ÜST SINIRDIR, ortalama değil:** ABD'nin
prepost'lu yoğunluğu (960 bar/seans) ve 252 seans/yıl tüm evrene
uygulandı. Gerçekte `THYAO.IS` gibi prepost'suz borsalar bunun altında
kalır; buna karşılık 7/24 enstrümanlar bu tahmini AŞAR — `BTC-USD` tek
başına yılda 525.600 adet `1m` bar üretir (252 × 960 = 241.920 varsayımının
iki katından fazla).

**Ve bu YILLIK büyümedir, tablo boyutu değil.** Arşiv kalıcı olduğu için
(K7) üçüncü yılın sonunda ~1,4 milyar satır / ~190 GB beklenir. Aşağıdaki
"464 milyon satırlı tabloda" gerekçeleri birinci yıl sonuna aittir;
sonraki yıllarda maliyet doğrusal artar.

`1m` tüm evrene açılsaydı o interval tek başına 1,21 milyar satıra /
~129 GB'a çıkardı (bugünkü 121 milyonun yerine, yani +1,09 milyar);
tablo toplamı yılda ~1,55 milyar satıra / ~180 GB'a (indeksle birlikte)
çıkardı — K6'nın gerekçesi.

### 5.3 Partitioning

```sql
ALTER TABLE price_bars
PARTITION BY RANGE COLUMNS (ts_utc) (
  PARTITION p_hist   VALUES LESS THAN ('2023-10-01'),
  PARTITION p2023_10 VALUES LESS THAN ('2023-11-01'),
  ...
  PARTITION p2028_09 VALUES LESS THAN ('2028-10-01')
);
```

- **Kural:** MySQL partition anahtarı, her unique key'in parçası olmalıdır.
  PK `(symbol, bar_interval, ts_utc)` bunu sağlar. Doğrulandı: bu PK ile
  tablo oluşur ve `EXPLAIN` aralık sorgusunda tek partition'a iner;
  `(symbol, bar_interval)` üzerinde ikinci bir UNIQUE anahtar denemesi
  `ERROR 1503` verir. Non-unique `ix_price_bars_local_date` ise legaldir —
  kısıt yalnız UNIQUE anahtarlar içindir.

- **`MAXVALUE` bölümü YOKTUR — bu bilinçli bir karardır.** İlk tasarımda
  vardı; ölçüm gerekçeyi tersine çevirdi:

  | | `MAXVALUE` ile | `MAXVALUE` olmadan |
  |---|---|---|
  | Bakım atlanırsa | satırlar sessizce `p_future`'a birikir, **pruning ölür** | insert `ERROR 1526` ile **gürültülü** başarısız olur |
  | Yeni ay eklemek | `ADD PARTITION` **imkânsız** (`ERROR 1493`), zorunlu olarak `REORGANIZE PARTITION` | `ADD PARTITION`, **metadata-only, anında** |
  | Onarım maliyeti | `p_future`'daki **her satır kopyalanır**, MDL altında | yok |

  `REORGANIZE`'ın `ALGORITHM=INPLACE, LOCK=NONE` seçeneği YOKTUR
  (`ERROR 1064`) — yani birkaç ay atlanmış bir bakım, yüz milyonlarca
  satırın kilit altında yeniden yazılması demektir. Sessiz performans
  çöküşü yerine gürültülü insert hatası tercih edilir: hata, bakımın
  atlandığını koşunun kendisinde bildirir.

- **Aralık ileriye VE geriye açılır.** Migration `2023-10`'dan `2028-09`'a
  kadar aylık partition açar. Geriye 36 ay zorunludur çünkü ilk dolum
  `60m` için 729 gün (2024'e kadar) veri getirir; tek bir başlangıç
  partition'ı olsaydı tüm geçmiş oraya düşer ve pruning geçmiş
  sorgularında hiç çalışmazdı. `1wk`/`1mo`'nun `period="max"` ile gelen
  daha eski satırları için tek bir `p_hist` bölümü yeterlidir — bu iki
  interval'in satır sayısı ihmal edilebilir (§5.2).

- Bakım işi yalnız İLERİ uçta çalışır ve `ADD PARTITION` kullanır (§7.5).

- Alembic partition DDL'ini autogenerate EDEMEZ; `op.execute()` ile elle
  yazılır. DDL, `models/bars.py`'de bir sabit olarak tutulur ve hem
  migration hem test conftest'i aynı sabiti kullanır — proje bunu
  `V_ACTIONS_CREATE` için zaten böyle yapıyor (§10).

### 5.4 `intraday_scope`

```python
class IntradayScope(Base):
    """1m (ve istenirse diger intervallerin) sembol alt kumesi.

    Bu liste KONFIGURASYON DEGIL VERIDIR (K6): 5.000 sembollu evrende
    500 sembollu bir alt kume .env'e sigmaz, surumlenmesi ve
    degistirilmesi gereken bir tablodur.
    """

    __tablename__ = "intraday_scope"
    __table_args__ = MYSQL_TABLE_ARGS

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    bar_interval: Mapped[str] = mapped_column(BarIntervalType(), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="1")
    added_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    note: Mapped[str | None] = mapped_column(String(255))
```

FK **taşır** — partition'lı değil, küçük bir tablo.

**Çözümleme kuralı** (asimetrik, bilinçli):

| interval | tabloda kayıt var | tabloda kayıt yok |
|---|---|---|
| `1m` | yalnız `enabled=1` semboller | **hiçbir sembol** |
| diğerleri | yalnız `enabled=1` semboller | **tüm evren** |

`1m`'in boş tabloda tüm evrene açılması 1,21 milyar satır/yıl demekti;
sessizce oraya kaymaktansa hiç çalışmaması güvenli taraftır. Diğer
intervaller için varsayılan tam evrendir çünkü hacimleri yönetilebilir.

**Kural `bar_interval` BAZINDA ve `enabled` değerinden BAĞIMSIZ
uygulanır:** "o interval için `intraday_scope`'ta en az bir satır var
mı?". Yalnız `enabled=0` satırları olan bir interval de "kayıt var"
sayılır ve hiçbir sembol koşmaz. Bunun tehlikeli bir sonucu var:
`5m` için tek bir test satırı eklemek diğer 4.999 sembolü sessizce
kapsam dışına atar. `yfin scope add --interval 5m` bu durumda uyarı
basar; `1m` dışındaki bir interval'e ilk kayıt eklenirken onay ister.

### 5.5 `bar_gaps`

```python
class BarGap(Base):
    """Kacirilan pencerelerin KALICI kaydi.

    Kalici arsivde "veri yok" iki farkli sey olabilir: piyasa kapaliydi,
    ya da biz kacirdik. Bu ayrim SONRADAN yapilamaz - Yahoo penceresi
    gectikten sonra "burada bar var miydi" sorusunun cevabi yoktur.
    Yazildigi anda kaydedilmezse bilgi geri gelmez (K10).
    """

    __tablename__ = "bar_gaps"
    __table_args__ = (Index("ix_bar_gaps_detected_at", "detected_at"), MYSQL_TABLE_ARGS)

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    bar_interval: Mapped[str] = mapped_column(BarIntervalType(), primary_key=True)
    gap_start_utc: Mapped[datetime] = mapped_column(TsType(), primary_key=True)
    gap_end_utc: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    reason: Mapped[str] = mapped_column(AsciiKeyType(24), nullable=False)
    # NULL = bosluk HALA ACIK. Planlayici acik bosluklari yeniden dener
    # (PB§6.2); pencere kapandiginda reason 'retention_expired'e cevrilir
    # ve resolved_at DAIMA NULL kalir - satir silinmez (K7).
    resolved_at: Mapped[datetime | None] = mapped_column(TsType())
```

`reason` değerleri: `retention_expired` (Yahoo penceresi geçmiş, veri
kalıcı olarak kayıp — `resolved_at` daima NULL), `fetch_failed` (istek
başarısız oldu, pencere hâlâ açık — planlayıcı yeniden dener).

`gap_start_utc` PK'nın parçasıdır: aynı sembol/interval için birden çok
boşluk olabilir ve her biri ayrı bir olaydır.

**Aynı anahtarın iki kez üretilmesi mümkündür** (bir dilim önce
`fetch_failed` olur, sonra pencere kapanınca `retention_expired`
sayılır). Yazım bu yüzden upsert'tür: `reason` ve `detected_at`
güncellenir, `resolved_at` dokunulmaz. `fetch_failed → retention_expired`
geçişi tek yönlüdür; tersi bir kayıptan geri dönüş anlamına gelirdi ve
mümkün değildir.

### 5.6 `bar_rescales`

```python
class BarRescale(Base):
    """Uygulanan geriye donuk olceklemelerin kaydi.

    IDEMPOTENCY KAPISIDIR: bu tablo olmadan ayni split ikinci kosuda
    tekrar uygulanir ve arsiv 10 kat daha bozulur. Kayit ile UPDATE
    AYNI TRANSACTION'DADIR (PB§6.6).
    """

    __tablename__ = "bar_rescales"
    __table_args__ = MYSQL_TABLE_ARGS

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    split_date: Mapped[date] = mapped_column(primary_key=True)
    ratio: Mapped[Decimal] = mapped_column(PriceType(), nullable=False)
    applied_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    rows_affected: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
```

PK `(symbol, split_date)` — `splits` tablosunun PK'sıyla birebir aynı;
"bu split uygulandı mı" sorusu tek anahtar aramasıdır.

`ratio` `DECIMAL(28,12)`'dir, float DEĞİL: 3:2 split'te `ratio = 1.5`
ve float bölme milyonlarca satırda birikimli sapma üretir.

### 5.7 Tip kararları

```python
def BarIntervalType() -> VARCHAR:  # noqa: N802
    """Interval kodu: '1m', '5m', '15m', '60m', '1wk', '1mo'.

    ENUM DEGILDIR (K5): ENUM'a deger eklemek ALTER TABLE'dir ve 464
    milyon satirli tabloda cok uzun surer. VARCHAR(4) ascii_bin yeni
    interval'i migration'siz kabul eder.

    KOLON ADI `bar_interval`, `interval` DEGIL: INTERVAL MySQL'de
    REZERVE KELIMEDIR (SQLAlchemy dogruladi). SQLAlchemy kolonu otomatik
    tirnaklar, ama view tanimi ve rescale UPDATE'i HAM SQL'dir; orada
    backtick bir gun unutulur ve hata uretim aninda cikar. PostgreSQL'de
    de INTERVAL bir tip adidir (PB§12), yani ad degisikligi gecise de
    hizmet eder.

    ascii_bin: PK bilesenidir; ascii_general_ci '1M' = '1m' sayar ve
    ayrica CHAR'in pad-space semantigi sondaki bosluğu kirpardi - ikisi
    de sessiz satir kaybi demektir (S5.1).
    """
    return VARCHAR(4, charset="ascii", collation="ascii_bin")
```

Geçerli değerlerin tek kaynağı Python tarafındadır:

```python
# datasets/bars.py
BAR_INTERVALS: tuple[str, ...] = ("1m", "5m", "15m", "60m", "1wk", "1mo")
BAR_LIMITS: dict[str, tuple[int | None, int | None]] = {
    # (istek basina azami gun, geriye azami derinlik) - PB§4.3'te olculdu
    "1m":  (8, 30),
    "5m":  (59, 59),
    "15m": (59, 59),
    "60m": (729, 729),
    "1wk": (None, None),
    "1mo": (None, None),
}
```

`bar_interval` kolonuna yazılan her değer bu tuple'a karşı doğrulanır;
doğrulama normalize'da, DB'de değil (K5'in bedeli, bilinçli).

### 5.8 İndeksler

| tablo | indeks | gerekçe |
|---|---|---|
| `price_bars` | PK `(symbol, bar_interval, ts_utc)` | Baskın sorgu: "bir sembolün bir interval'inin bir aralığı". Clustered index bunu tam karşılar. |
| `price_bars` | `ix_price_bars_local_date (local_date)` | "Şu gün hangi sembollerde bar var" ve boşluk denetimi. |
| `bar_gaps` | `ix_bar_gaps_detected_at` | Aylık rapor son tespitleri okur. |
| `intraday_scope`, `bar_rescales` | yalnız PK | Küçük tablolar; ikinci indeks yazma maliyetini boşuna artırırdı. |

**`price_bars`'a `(bar_interval, ts_utc)` indeksi EKLENMEZ.** InnoDB'de
her secondary index PK'yı (≈19 B) taşır; 464 milyon satırda bu ~14 GB'lık
bir indekstir ve karşıladığı sorgu ("tüm sembollerde şu an") partition
pruning + tam tarama ile zaten kabul edilebilir sürede döner. Ölçülmüş
bir ihtiyaç doğarsa eklenir; şimdi eklemek spekülatiftir.

**`ix_price_bars_local_date` de aynı ~14 GB'a mal olur** ve bu maliyet
§5.2 projeksiyonuna DAHİL EDİLMİŞTİR. Farkı, karşıladığı sorgunun
zorunlu olmasıdır: aylık boşluk denetimi ve öksüz satır raporu
(§7.5) `local_date` üzerinden çalışır ve bunlar olmadan FK'sız tablonun
bütünlüğü hiç doğrulanamaz. İkinci bir indeks eklenmeden önce bu
maliyet tekrar tartılmalıdır.

### 5.9 `sync_runs` değişikliği

Şema değişikliği YOK. `sync_run_items.dataset` yeni adları (`bars_1m` …
`bars_1mo`) olduğu gibi kabul eder. Kapsam dışı semboller için `status`
kolonuna yeni bir StrEnum değeri eklenir: `ItemStatus.OUT_OF_SCOPE`
(§6.5c). Kolon tipi değişmediği için bu bir şema değişikliği DEĞİLDİR.

### 5.10 View

`v_price_bars_regular`: yalnız normal seans barları.

```sql
CREATE OR REPLACE SQL SECURITY INVOKER VIEW v_price_bars_regular AS
  SELECT symbol, bar_interval, ts_utc, local_date, open, high, low, close, volume
    FROM price_bars
   WHERE is_extended = 0
```

Amacı kolaylık değil, KAZA ÖNLEME: `is_extended` filtresini unutmak,
seans dışı düşük hacimli barları normal seansa karıştırır ve bu, hesaplanan
her göstergeyi sessizce bozar. Varsayılan okuma yolu view olmalıdır.

---

## 6. Mimari

### 6.1 `IntervalBarDataset` — tek sınıf, altı kayıt

```python
class IntervalBarDataset(Dataset[BarPayload]):
    """Tek bir interval'in price_bars'a yazilmasi. Ad: bars_<interval>.

    Alti interval icin alti sinif yazmak ayni govdenin alti kopyasidir
    (K9). Interval bir ORNEKLEME PARAMETRESIDIR.
    """

    produces = ("price_bars", "bar_gaps")
    # SADECE "symbols". Once ("symbols", "splits") yazilmisti; iki ayri
    # nedenle YANLISTI:
    #   1. "splits" bir TABLO adidir, dataset adi degil - Registry
    #      cozumlemeyi dataset adlariyla yapar, cozumleme patlardi.
    #   2. SplitsDataset._SeriesDataset.fetch -> fetch_history_frame,
    #      yani TAM bir 1d history() cagrisi. `--datasets bars_1m` bile
    #      1d cekerdi. Proje bu sekle acikca karsi:
    #      corporate_actions.py:36-39 ayni gerekcyele
    #      `depends_on = ("history",)` YAPMIYOR.
    # Rescale kancasi (PB§6.6) dataset bagimliligiyla degil, `splits`
    # TABLOSUNU okuyarak calisir; boylece `history` o kosuda hic
    # secilmemis olsa bile DB'deki mevcut split'lere gore dogru is yapar.
    depends_on = ("symbols",)
    # Aralik yfinance CAGRISINA gecer -> gercek geriye donuk cekim
    # (AH S6.2). --start/--end watermark'i gecersiz kilar.
    date_range = "api"

    def __init__(self, interval: str) -> None:
        if interval not in BAR_INTERVALS:
            raise ValueError(f"bilinmeyen interval: {interval}")
        self.interval = interval
        self.name = f"bars_{interval}"


for _iv in BAR_INTERVALS:
    register(IntervalBarDataset(_iv))
```

**Sözleşme notu — `name` artık instance attribute'tur.** `datasets/base.py`
`produces` için "Class attribute'tur, property DEĞİL: alt tipte property'ye
çevrilirse tabanın sözleşmesi daralır (S §6.1/6)" notunu taşıyor. `name`'in
instance attribute'a çevrilmesi bu dersi ihlal ETMEZ (daralma yok, `str`
yine `str`; `Registrable` protokolü sağlanmaya devam eder) ama `Dataset`
docstring'i buna göre güncellenmelidir — aksi halde bir sonraki okuyan
`name`'i sınıf düzeyinde arar ve bulamaz.

`Registry.register` `ds.name`'i okur; instance attribute ile sorunsuz
çalışır, değişiklik gerekmez.

**Alias'lar** (`registry.py`):

```python
"bars":     ("bars_1m", "bars_5m", "bars_15m", "bars_60m", "bars_1wk", "bars_1mo"),
"intraday": ("bars_1m", "bars_5m", "bars_15m", "bars_60m"),
```

### 6.2 Pencere planlayıcı — `plan_windows`

Bu fonksiyon, tasarımın veri kaybına karşı ilk savunma hattıdır.

```python
@dataclass(frozen=True)
class FetchPlan:
    windows: tuple[tuple[date, date], ...]   # cekilecek dilimler
    gap: tuple[datetime, datetime] | None    # KURTARILAMAYAN bosluk


def plan_windows(
    interval: str,
    watermark: datetime | None,
    now: datetime,
    *,
    start: date | None = None,
    end: date | None = None,
    open_gaps: Sequence[tuple[datetime, datetime]] = (),
) -> FetchPlan: ...
```

**Tampon YOKTUR.** İlk taslak "her dilimi bir gün daralt" diyordu ve bu,
dokümanın beş ayrı yerindeki "`1m` → 4 dilim" ifadesiyle çelişiyordu
(7 günlük dilimle 30 gün 5 dilim eder). Çelişki `BAR_LIMITS`'in ne
olduğunu netleştirerek çözülür: **`BAR_LIMITS` ölçümde KABUL EDİLEN
değerleri taşır** (8, 59, 729 — 9, 60 ve 730 reddedildi, §4.3), yani
tampon zaten sabitlerin içindedir. Planlayıcı üzerine ikinci bir tampon
UYGULAMAZ. `1m` ilk dolumu: `ceil(30 / 8) = 4` dilim.

Beş senaryo, beşi de test edilir:

1. **İlk dolum** (`watermark is None`): derinlik kadar geriye gidilir,
   istek penceresine bölünür. `1m` → 4 dilim. Diğerleri → 1 dilim.
2. **Normal artımlı**: `start = watermark - overlap`. Tek dilim.
3. **Duraklama sonrası**: Sistem 10 gün durursa `1m` watermark'ı 10 gün
   geridedir ve naif `start = watermark - overlap` **8 günlük sınırı
   aşar → istek `YFPricesMissingError` ile DÜŞER** (§4.1/9). Yani
   duraklama, tüm dilimi kaybettirirdi. Planlayıcı bunu 2 dilime böler.
4. **Derinlik aşımı**: Sistem 35 gün durursa ilk 5 gün Yahoo'da artık
   yoktur. Planlayıcı çekilebilir kısmı dilimler VE
   `gap = (watermark, now - depth)` döndürür; bu `bar_gaps`'e
   `retention_expired` olarak yazılır. Sınırlar `BAR_LIMITS`'ten
   okunduğu için `gap`'in bitişi ile ilk dilimin başlangıcı BİRBİRİNE
   BİTİŞİKTİR — arada kaydedilmeyen bir gün kalmaz.
5. **Açık boşlukların yeniden denenmesi** (`open_gaps`): Planlayıcı
   yalnız watermark'a bakarsa, ortadaki bir dilim düşüp sonraki dilimler
   yazıldığında watermark boşluğun ÖTESİNE geçer ve o pencere bir daha
   hiç istenmez — `bar_gaps`'e `fetch_failed` yazmak tek başına hiçbir
   şeyi kurtarmaz. Bu yüzden planlayıcı, `bar_gaps`'teki
   `reason='fetch_failed' AND resolved_at IS NULL` satırlarının hâlâ
   Yahoo penceresi içinde kalan kısımlarını da dilimler. Pencere yeniden
   çekilip yazıldığında `resolved_at` doldurulur; pencere bu arada
   kapandıysa satır `retention_expired`'a çevrilir ve bir daha denenmez.

**`--start`/`--end` verildiğinde** (`date_range = "api"`): watermark ve
`open_gaps` yok sayılır, verilen aralık yine istek penceresine göre
dilimlenir, ve derinliği aşan kısım için **`gap` ÜRETİLMEZ** — elle
istenen geriye dönük bir çekimin başarısız olması bir "kaçırma" değildir,
kullanıcı hatasıdır ve loglanır.

`overlap`: `yf_bar_overlap_days` (varsayılan 2). Örtüşme idempotenttir
(§4.1/5), maliyeti yalnız birkaç yüz satırlık gereksiz upsert'tür — ama
seans sınırındaki bir barın kaçmasını önler.

### 6.3 Watermark'a interval boyutu — mevcut koda dokunma

Mevcut `WatermarkReader.__call__(table, column, symbol)`
`MAX(column) WHERE symbol = ?` sorguluyor (`runner.py:128-135`).
`price_bars`'ta bu YANLIŞ CEVAP VERİR: `1m` güncelken `60m` iki yıl
geride olabilir; tek bir maksimum, `bars_60m`'i "güncel" sanıp ilk
dolumu hiç yapmaz.

```python
def __call__(
    self, table: str, column: str, symbol: str,
    *, where: Mapping[str, Any] | None = None,
) -> date | datetime | None:
    target = Base.metadata.tables[table]
    conditions = [target.c["symbol"] == symbol]
    for col, value in (where or {}).items():
        conditions.append(target.c[col] == value)
    stmt = select(func.max(target.c[column])).where(and_(*conditions))
    ...
```

`SyncContext.watermark` aynı parametreyi geçirir; `watermark_provider`
tipi `Callable[..., date | datetime | None]` olarak genişler.

**Değişen çağrı noktaları:** `runner.py` (`WatermarkReader`),
`datasets/base.py` (`SyncContext.watermark` ve provider tipi). Mevcut
çağrıların tümü `where=None` ile davranışını korur — `history.py`'nin
`FRAME_CONSUMERS` mantığı etkilenmez.

`IntervalBarDataset` kullanımı:

```python
mark = ctx.watermark("price_bars", "ts_utc", where={"bar_interval": self.interval})
```

### 6.4 `BarPayload` ve `is_extended`

```python
@dataclass(frozen=True)
class BarPayload:
    frame: pd.DataFrame
    trading_periods: pd.DataFrame | None
    interval: str
```

`fetch` içinde `ctx.ticker.get_history_metadata()` çağrılır — ölçüldü,
**ek ağ isteği değildir** (§4.1/7).

`has_prepost` alanı payload'da YOKTUR ve kurala GİRMEZ. İlk tasarımda
`has_pre_post_market_data=False` bir erken çıkış kapısıydı; ölçüm bunu
çürüttü (§4.5/1): `SHEL.L` ve `VWCE.DE` bu alanı `False` bildirdikleri
hâlde `prepost=True` ile 5 ve 8 seans dışı bar döndürüyor. O kapı,
`SHEL.L`'in 16:30/16:35 barlarını normal seans olarak damgalar ve
`v_price_bars_regular`'a sokardı.

**Tek doğruluk kaynağı `tradingPeriods`'ın `start`/`end` aralığıdır:**

```
1. bar_interval intraday DEGIL ('1wk','1mo')   -> 0  (kavram anlamsiz)
2. trading_periods yok ya da o gunu icermiyor  -> 0  (guvenli varsayilan)
3. bar_ts < start veya bar_ts >= end           -> 1
4. aksi halde                                  -> 0
```

Kural 3 ölçülen üç durumu da doğru sınıflandırır: `THYAO.IS`
(`start=09:30, end=18:00`, ilk bar 09:55 → hepsi 0), `SHEL.L`
(`start=08:00, end=16:30` → 16:30 ve 16:35 barları 1), `BTC-USD`/`GC=F`
(`start=00:00, end=23:59` → hepsi 0), `AAPL` (`start=09:30, end=16:00` →
04:00–09:25 ve 16:00–19:55 barları 1).

**`pre_*`/`post_*` kolonları HİÇ KULLANILMAZ.** Dejenere oldukları
ölçülen kolonlar bunlardır (`THYAO.IS`: `pre_start == pre_end`), ve
`start`/`end` zaten normal seansı tam olarak tanımlıyor. Ayrıca bu
kolonlar `prepost=False` ile yapılan bir çağrıda HİÇ GELMEZ (§4.5/3) —
onlara dayanan kod, `price_bars` dışından çağrıldığında `KeyError`
verirdi. Normalize, kolon varlığını yine de kontrol eder.

Kural 2'nin "güvenli varsayılan"ı bilinçli olarak `0`'dır: bilinmeyen bir
barı seans dışı saymak onu `v_price_bars_regular`'dan gizlerdi; ters
hata (seans dışını normal saymak) daha görünür ve denetimde yakalanır.

### 6.5 Kapsam kapısı — ağa çıkmadan önce

```python
def fetch(self, ctx: SyncContext) -> BarPayload:
    if not ctx.in_scope(self.interval):
        raise DatasetOutOfScope(self.interval)   # ag yok, istek yok
    ...
```

Üç somut mekanizma gerekiyor; hiçbiri mevcut kodda hazır değil.

**(a) `ScopeReader` — `SyncContext`'in DB erişimi yok.** `SyncContext`
hiçbir session tutmuyor; tek DB kanalı enjekte edilen
`_watermark_provider`. Ayrıca `_worker` her SEMBOL için yeni bir
`SyncContext` kuruyor, dolayısıyla "worker başına bir kez `ctx.cached`"
diye bir şey mümkün değil — o, sembol başına bir `intraday_scope`
sorgusu (koşu başına ~5.000) olurdu.

Çözüm `WatermarkReader`'ın desenini birebir izler: kendi kısa oturumunu
açan, `threading.Lock` taşıyan ve **kapsam kümesini kendi örneğinde
önbelleğe alan** bir `ScopeReader` sınıfı `runner.py`'ye eklenir ve
`SyncContext`'e ikinci bir provider olarak enjekte edilir. Kapsam kümesi
koşu başına bir kez okunur. Shard child process'lerinin her biri kendi
örneğini alır (P §4).

**(b) `DatasetOutOfScope` mevcut akışta `FAILED` olur.** `_worker`'ın
dataset döngüsü `fetch`/`normalize`'ı tek bir `except Exception` ile
sarıyor → `classify_error` → `payload.failures` → `ItemStatus.FAILED`,
ve `payload.error_kinds` proxy sağlık takibini besliyor. Kapsam dışı bir
sembolün proxy'yi cezalandırması saçmadır. Gerekenler:

- `DatasetOutOfScope` **`ValueError`'dan TÜREMEZ**: `errors.py`
  `ValueError`'ı `_NEVER_RETRYABLE` sayıyor ve istisna sessizce bir veri
  hatası olarak sınıflanırdı. Nötr bir tabandan türer.
- `_worker`'a jenerik `except`'ten **ÖNCE** gelen bir
  `except DatasetOutOfScope` dalı ve `SymbolPayload`'a üçüncü bir kanal
  (bugün yalnız `failures` ve `skipped` var).

**(c) `ItemStatus.OUT_OF_SCOPE` — `not_attempted` YENİDEN KULLANILAMAZ.**
İlk tasarım `not_attempted`'ı öneriyordu; bu, koşuyu her gün başarısız
gösterirdi. `runner.py`'de:

```python
if self.failed == 0:
    return EXIT_PARTIAL if self.not_attempted else EXIT_OK
```

docstring'iyle birlikte: *"İŞLENMEMİŞ SEMBOL VARKEN ÇIKIŞ KODU ASLA 0
OLAMAZ (P §8.1)"*. `not_attempted`'ın mevcut anlamı "shard kendini geri
çekti, bu semboller İŞLENMEDİ" — yani gerçek bir eksikliktir. Kapsam
dışılık ise kasıtlı bir karardır. 4.500 kapsam dışı `bars_1m` hücresine
`not_attempted` yazmak `yfin sync`'i her gün exit 2 ve
`sync_runs.status='partial'` yapardı.

Yeni bir `ItemStatus.OUT_OF_SCOPE` eklenir; `RunTally.cells` ve
`exit_code` onu `empty` gibi normal sayar. Bu, `sync_run_items` şemasına
dokunmaz (`status` bir StrEnum kolonudur).

Ayrım şart: bu olmadan aylık denetimde "500 sembolde `1m` var, 4.500'ünde
yok" tablosu, gerçek bir arızayla ayırt edilemez.

### 6.6 Geriye dönük yeniden ölçekleme — `rescale.py`

Bu, tasarımın en kritik parçasıdır (§4.6'nın kanıtladığı sorun) ve en
kolay yanlış yazılan parçasıdır.

**Formülün yönü doğrulandı.** Arşive split'ten ÖNCE yazılmış bir NVDA
satırı split öncesi ölçektedir (`Close ≈ 1224.40`, `Volume ≈ 52,84 M`);
Yahoo bugün aynı barı `122.44` / `528,4 M` olarak veriyor.
`close / ratio = 1224.40 / 10 = 122.44` ✓ ve
`volume × ratio = 52,84 M × 10 = 528,4 M` ✓. Ters split (`ratio = 0.1`)
aynı formülle doğru yönde çalışır; özel dal yoktur.

#### Kurulum tohumu (`rescale --seed`) — bu adım atlanırsa arşiv yok olur

`splits` tablosu mevcut hat tarafından ZATEN DOLUDUR (AAPL'in 1987, 2000,
2005, 2014, 2020 split'leri dahil). Tetikleyici "`bar_rescales`'te
karşılığı olmayan her split" olduğu için, boş bir `bar_rescales` ile
yapılan İLK KOŞU tüm tarihsel split'leri uygular ve AAPL arşivini
2·2·2·7·4 = **224'e böler**. Oysa o barlar Yahoo'dan zaten güncel
ölçekte gelmiştir (§4.6) — yani mekanizma, önlemek için var olduğu
bozulmayı kendi eliyle yapar.

Bu yüzden migration'ın hemen ardından, tek seferlik bir tohumlama adımı
çalışır (§10 adım 9a): `splits` tablosundaki HER satır için
`bar_rescales`'e `ratio = 1, rows_affected = 0` "baseline" kaydı yazılır.
Hiçbir UPDATE çalıştırılmaz. Böylece arşiv çekilmeden önceki split'ler
kalıcı olarak "uygulanmış" sayılır ve bir daha asla değerlendirilmez.

`--seed` idempotenttir ve `bars_*` ilk kez koşmadan ÖNCE çalışmak
zorundadır; sırası §10'da bağlayıcıdır.

#### Ne zaman çalışır — İKİ kapı

`bars_*` yazımından ÖNCE, sembol başına. Bir split'in uygulanabilmesi
için **ikisini birden** geçmesi gerekir:

1. **`bar_rescales`'te kaydı yok** — idempotency.
2. **`split_date` > sembolün `price_bars`'taki en erken `local_date`'i** —
   *yapısal koruma*.

İkinci kapı uygulama sırasında eklendi ve tasarımın en kırılgan yerini
kapatıyor. İlk hâlinde doğruluk **tek başına operasyonel bir adıma**
(`rescale --seed`) yaslanıyordu: taze bir kurulumda seed atlanırsa ilk
koşu `splits`'teki tüm tarihsel split'leri uygular ve Yahoo'dan **zaten
güncel ölçekte gelmiş** barları yeniden böler. Bir insan adımını
unutmanın bedeli, geri alınamaz bir arşiv bozulması olamaz.

Arşivden eski bir split'in yapacak işi zaten yoktur — o barlar split
sonrası ölçekte geldi. Sembolün hiç barı yoksa (`earliest` NULL) koşul
NULL döner ve satır elenir; doğrusu budur.

`rescale --seed` **hâlâ anlamlıdır** (denetim izi ve niyet beyanı,
`bars maintain` onu kontrol eder) ama artık doğruluğun tek dayanağı
değildir.

Tetikleyici bir dataset bağımlılığı DEĞİLDİR (§6.1) — `history` o koşuda
seçilmemiş olsa bile DB'deki mevcut split'lere göre doğru davranır.

#### Ne yapar

```sql
UPDATE price_bars
   SET open   = open   / :ratio,
       high   = high   / :ratio,
       low    = low    / :ratio,
       close  = close  / :ratio,
       volume = FLOOR(volume * :ratio)
 WHERE symbol = :symbol
   AND ts_utc < :split_ts
   AND bar_interval IN ('1m', '5m', '15m', '60m');
```

Dokuz nokta, her biri ölçülmüş bir tuzağa karşılık gelir:

1. **`1wk`/`1mo` KAPSAM DIŞIDIR.** Bu iki interval her koşuda
   `period="max"` ile baştan çekilir, yani daima Yahoo'nun güncel
   ölçeğindedir. Ölçekleme onlara uygulanırsa ve o koşuda `bars_1wk`
   fetch'i düşerse, satırlar **çift düzeltilmiş** kalır ve
   `bar_rescales` split'i "uygulandı" olarak kaydettiği için bir daha
   düzelmez.
2. **`:split_ts`, split gününün YEREL 00:00'ının UTC karşılığıdır.**
   `bar_rescales.split_date` bir `DATE`; ham UTC gece yarısı alınırsa
   pozitif ofsetli borsalarda (BIST +03) split günü sabahının barları
   yanlış tarafta kalır. Sembolün tz'si `history_metadata`'dan okunur —
   `price_history.session_date`'in tanımıyla (S §5.4) aynı ders.
3. **`ratio <= 0` REDDEDİLİR.** `splits`'te bozuk bir `0` satırı
   `ERROR_FOR_DIVISION_BY_ZERO` üretir ve tüm sembolü düşürür. Rescale
   böyle bir satırı uygulamaz, `bar_rescales`'e de yazmaz; hata
   seviyesinde loglar ve devam eder.
4. **`ratio` DECIMAL'dir**, float değil (K4 / §5.6).
5. **`volume` FLOOR ile tam sayıya iner.** Ölçüldü: FLOOR olmadan
   `volume = 3, ×1.5 → 4.5` MySQL'de `5` olarak yazılır —
   `STRICT_TRANS_TABLES` altında bile sessizce, uyarısız. Yuvarlama
   Python/SQL tarafında BİLİNÇLİ yapılır (AH §5.7 / F §5.6'nın `quantize`
   dersi).
6. **Bölme GERİ DÖNDÜRÜLEMEZ ŞEKİLDE KAYIPLIDIR.** Ölçüldü:
   `DECIMAL(28,12)` üzerinde `1200.123456789012 / 10 × 10 =
   1200.123456789010`. 12. ondalık basamak her ölçeklemede aşınır. Bu
   KABUL EDİLEN bir maliyettir — alternatif (ham fiyatı ayrı saklamak)
   satır boyutunu ikiye katlardı ve Yahoo zaten ham fiyat vermiyor. Fiyat
   verisinde 12. ondalık anlamsızdır; kaydın kendisi (`bar_rescales`)
   kaç kez ölçeklendiğini gösterir.
7. **`ts_utc < :split_ts` partition pruning'den yararlanır** (ölçüldü:
   `EXPLAIN UPDATE` 4 partition'dan 2'sine iniyor). Ancak `WHERE`'de
   `bar_interval` bir aralık olduğu için PK yalnız `symbol` önekiyle
   kullanılır; `ts_utc` bir indeks aralığı değil filtredir. Split nadir
   olduğu için bu kabul edilir.
8. **Slot, kilitle değil INSERT ile iddia edilir.** İlk tasarım
   `SELECT ... FOR UPDATE` öneriyordu; ölçüm bunun ÇALIŞMADIĞINI
   gösterdi: var olmayan bir PK üzerinde `FOR UPDATE` yalnız bir *gap
   lock* alır, gap lock'lar birbiriyle uyumludur, iki oturum da "satır
   yok, uygulayacağım" der ve çakışma INSERT anında
   `ERROR 1213 (deadlock)` olarak patlar. Doğrusu: **önce
   `INSERT ... ON DUPLICATE KEY UPDATE` ile slotu al, `ROW_COUNT()`'a
   göre dallan**; slot bizimse UPDATE'i çalıştır.
9. **UPDATE ile kayıt AYNI TRANSACTION'DADIR** ve bu transaction,
   sembolün kendi yazma transaction'ıdır (§8.6). Ayrılırsa çöken bir
   koşu ya ölçeklenmiş ama kayıtsız (ikinci koşuda TEKRAR ölçeklenir)
   ya da kaydedilmiş ama ölçeklenmemiş bir arşiv bırakır.

#### Sıralama garantisi

`rescale`, sembolün yazma transaction'ı içinde, `bars_*` `TableWrite`'
larından ÖNCE koşar. Ters sırada, aynı koşuda yazılan yeni barlar (zaten
yeni ölçekte) bir kez daha bölünürdü.

`_persist_symbol` bugün `payload.results`'ı registry sırasında geziyor ve
kanca kavramı YOK; bu yüzden ona açık bir kanca noktası eklenir. Kancanın
sembol transaction'ının İÇİNDE olması, §8.6'nın "sembol başına tek
transaction: ya bütün olarak yazılır ya hiç" invariant'ını korur —
`_persist_with_retry`'ın kilit çakışmasında tüm bloğu yeniden çalıştırması
da böylece güvenli kalır (rescale henüz commit edilmemiştir).

### 6.7 Yazma katmanı — `INSERT_CHUNK`

`bars_1m` ilk dolumda dilim başına 8 gün × ~5-6 seans × ~960 bar ≈
**5.500 satır**, sembol başına toplam ~20.000 satır üretir (30 takvim
günü ≈ 21 seans; §5.2'nin 252 seans/yıl kabulüyle tutarlı). Dilimler ayrı
`TableWrite`'lardır (§8.1), yani tek bir yazımda 20.000 satırın tamamı oluşmaz —
ama en kötü durumda tek `TableWrite` yine binlerce satır taşır ve mevcut
`apply_write` bunu tek `INSERT`'e koyar.

Gerekçe **paket boyutu değildir**: `max_allowed_packet` varsayılanı
64 MB, bu şekildeki 20.000 satır ise ~3-4 MB'lık ifade metnidir. İlk
taslaktaki bu argüman yanlıştı. Gerçek iki gerekçe:

- **Kilit süresi:** tek dev `INSERT` shard'lar arası kilit süresini
  uzatır ve `_persist_with_retry`'ın yeniden deneme penceresini büyütür.
- **Ya hep ya hiç:** kısmi başarısızlıkta 20.000 satırın tamamı geri
  alınır; parça parça yazım, çöken bir koşudan sonra elde daha çok veri
  bırakır.

**Uygulama detayı — `align_rows` sırası kritik:** `MySQLRowWriter.write`
önce tüm satırlar üzerinde `align_rows` çağırıp birleşik kolon setini
hesaplıyor, sonra `ON DUPLICATE KEY UPDATE` haritasını `rows[0]`'dan
türetiyor. Naif dilimleme (`rows[i:i+N]` üzerinde döngü) farklı
dilimlerin farklı kolon setiyle ve farklı update haritasıyla yazılmasına
yol açar. **`align_rows` TÜM listeye, dilimlemeden ÖNCE uygulanmalıdır.**
Doğrulama (`_verify`) etkilenmez; o zaten `VERIFY_CHUNK` ile parçalı
çalışıyor ve yazımın sonunda bir kez çağrılıyor.

`INSERT_CHUNK` bir **modül sabitidir** (`VERIFY_CHUNK` ile simetrik),
`.env` anahtarı DEĞİLDİR. `persistence.py` bugün `yfin.config`'ten hiçbir
şey import etmiyor ve `MySQLRowWriter.__init__` yalnız bir `Session`
alıyor; ayarlanabilir yapmak bu katmana yapılandırma bağımlılığı sokardı.
(İlk taslak hem sabit hem `YF_BAR_INSERT_CHUNK` anahtarı tanımlıyordu;
anahtar kaldırıldı.)

Bu `price_bars`'a özgü DEĞİLDİR — mevcut tüm dataset'ler için doğru
davranıştır; bugüne kadar kimse bu boyutta satır üretmediği için ortaya
çıkmamıştır.

### 6.8 Yeni API ekleme yolu (S §6.4 ile uyum)

Yeni bir interval eklemek: `BAR_INTERVALS` ve `BAR_LIMITS`'e satır
eklemek. Migration GEREKMEZ (K5). Yeni bir tablo veya sınıf da gerekmez.

---

## 7. Veri akışı

### 7.1 Sembol tarafı

```
symbols (bootstrap)
   |
   +-- history        -> price_history, dividends, splits, capital_gains  [DEGISMEDI]
   |                        |
   |                        +-- rescale kancasi -> price_bars UPDATE + bar_rescales
   |
   +-- bars_1m   \
   +-- bars_5m    \
   +-- bars_15m    >-- price_bars   (+ bar_gaps)
   +-- bars_60m   /
   +-- bars_1wk  /
   +-- bars_1mo /
```

Her `bars_*` dataset'i bağımsızdır: biri düşerse diğerleri yazar. Paylaşılan
tek şey `intraday_scope` okuması (worker başına önbelleklenir).

**`history` çerçevesi PAYLAŞILMAZ.** `bars_*` kendi `interval` parametresiyle
ayrı çağrı yapar; `history.py`'deki `CACHE_HISTORY` önbelleği `1d`'ye
özgüdür ve dokunulmaz.

### 7.2 İlk dolum ve artımlı koşu

| interval | ilk dolum | artımlı | not |
|---|---|---|---|
| `1m` | 4 istek (30 gün / 8) | 1 istek | duraklama > 8 gün → 2+ dilim |
| `5m` | 1 istek (59 gün) | 1 istek | |
| `15m` | 1 istek (59 gün) | 1 istek | |
| `60m` | 1 istek (729 gün) | 1 istek | |
| `1wk` | 1–3 istek (`period="max"`) | 1–3 istek | sayım kararsız, §4.4 |
| `1mo` | 1–2 istek (`period="max"`) | 1–2 istek | sayım kararsız, §4.4 |

**Koşu sıklığı bir gereksinimdir, tercih değil:** `1m` için istek
penceresi 8 gündür. Sistem 8 günden uzun durursa planlayıcı çoklu dilime
böler, 30 günden uzun durursa veri KALICI olarak kaybolur. Günlük koşu
zorunludur; `bar_gaps` bu zorunluluğun ihlal edildiğini görünür kılar.

### 7.3 Idempotency

Ölçüldü (§4.1/5): örtüşen pencerede 78 ortak bar, `Open/Close/Volume`
birebir eşit.

```python
UPDATE_COLUMNS = ("local_date", "open", "high", "low", "close", "volume", "is_extended")
```

`monotonic_columns` YOK (`is_repaired` kolonu yok).

**Rescale ile etkileşimi — dikkat edilmesi gereken tek yer:** Bir split
uygulandıktan sonra, aynı sembolün ESKİ bir penceresi yeniden çekilirse
Yahoo zaten yeni ölçekte veri verir ve upsert doğru değeri yazar. Ters
durum (rescale'den önce yazılmış bar) `bar_rescales` sayesinde ikinci kez
ölçeklenmez. İki mekanizma çakışmaz.

### 7.4 İstek bütçesi (§4.4 ölçümüyle, `repair` kapalı)

Sayımlar sembole ve önbellek durumuna göre değiştiği için (§4.4) bütçe
ÜST SINIR üzerinden kurulur; gerçek yük tipik olarak bunun %60-70'idir.

| iş | sembol | istek/sembol (tipik–üst sınır) | üst sınır toplam |
|---|---|---|---|
| `1d` (mevcut hat, `repair=True`) | 5.000 | 2 | 10.000 |
| `bars_5m,15m,60m` | 5.000 | 3–6 | 30.000 |
| `bars_1m` | 500 | 1–2 | 1.000 |
| **günlük toplam** | | | **~41.000** |
| `bars_1wk,1mo` (**haftalık**, §11) | 5.000 | 2–4 | +20.000 |
| **haftanın en yoğun günü** | | | **~61.000** |
| ilk dolum eki (`1m`, 30 gün = 4 dilim) | 500 | +3 | +1.500 (tek sefer) |

`yf_rate_limit_per_sec = 2.0` **shard BAŞINA** uygulanır (her shard ayrı
process, kendi limiter'ı — P §4). Tek shard'da günlük ~5,7 saat, en
yoğun günde ~8,5 saat; **4 shard ile sırasıyla ~1,4 ve ~2,1 saat.**

`1wk`/`1mo`'nun günlük toplamda OLMAMASI §11 koşu takvimiyle
kasıtlıdır: bar sınırları haftada/ayda bir kapanır, günlük çekmek aynı
satırı yeniden yazmaktan başka bir şey yapmaz.

**`repair` açık olsaydı:** `5m` ve `15m` için sembol başına +12 ek istek
(1→6 ve 1→8, §4.4) → 5.000 sembolde **+60.000 istek/gün**, yani günlük
bütçenin kendisinin ~1,5 katı bir EK yük; günlük toplam 41.000'den
~101.000'e, yani ~2,5 katına çıkardı. K8'in sayısal gerekçesi budur. (İlk taslakta bu hesap
bir yerde 500, başka yerde 5.000 sembolle yapılmıştı ve "toplam istek"
ile "ek istek" karıştırılmıştı.)

### 7.5 Aylık bakım işi — `yfin bars maintain`

**Silme YAPMAZ** (K7). Dört iş:

1. **Partition ilerletme.** Gelecek 12 ayın partition'ı var mı; yoksa
   `ADD PARTITION` ile ekle — `MAXVALUE` bölümü olmadığı için bu işlem
   metadata-only ve anlıktır (§5.3). Atlanırsa yeni satırlar
   `ERROR 1526` ile reddedilir; koşu gürültülü şekilde düşer ve eksiklik
   aynı gün görülür.
2. **Öksüz satır denetimi.** FK'yı partition için feda ettik (K2); bu
   sorgu onun yerini tutar:
   `SELECT DISTINCT symbol FROM price_bars WHERE symbol NOT IN (SELECT symbol FROM symbols)`.
   Bulgu varsa RAPORLANIR, silinmez — silme kararı insana aittir.
3. **Boşluk raporu.** Son ayın `bar_gaps` satırları, `reason` kırılımıyla.
4. **Kapsam tutarlılığı.** `intraday_scope`'ta olup `symbols`'ta olmayan
   veya `symbols`'ta delisted işaretli semboller.

PostgreSQL/TimescaleDB'ye geçişte **1. iş silinir** (chunk yönetimi
otomatiktir), **2. iş silinir** (FK geri gelir), 3 ve 4 kalır (§12).

---

## 8. Hata yönetimi ve veri bütünlüğü

### 8.1 İzolasyon sınırı

Değişmedi (S §8.1): bir dataset'in bir semboldeki hatası o hücreyle
sınırlıdır. `bars_5m` düşerse `bars_15m` yazmaya devam eder.

Yeni bir alt sınır var: **`1m`'in dört dilimi birbirinden bağımsızdır.**
İkinci dilim düşerse birinci, üçüncü ve dördüncü yazılır; düşen dilimin
aralığı `bar_gaps`'e `fetch_failed` olarak girer. Tüm fetch'i tek bir
atomik birim saymak, bir ağ hatasında 30 günün tamamını kaybettirirdi.

### 8.2 `empty` ≠ `failed` ≠ `out_of_scope`

Bu bölümün ilk taslağı, ölçüm hatasına dayanan yanlış bir öncülden
kurulmuştu: "Yahoo'nun pencere reddi exception fırlatmaz". §4.1/9
düzeltmesi bunu tersine çevirdi — **bu projenin yapılandırmasında
(`hide_exceptions=False`) pencere reddi `YFPricesMissingError` fırlatır.**

Sonuç, ayrımı zorlaştırıyor: aynı istisna tipi hem "pencere dışı istek"
hem "bu aralıkta bar yok" için kullanılıyor. Tip'e bakarak ayırmak
mümkün değildir.

| durum | ayırt etme | kayıt |
|---|---|---|
| Pencere aşımı | `plan_windows` sınırı bilir; **bu istek hiç yapılmamalı** | assertion — kod hatası |
| Yahoo penceresi geçmiş | planlayıcı ağa çıkmadan hesaplar | `bar_gaps.reason='retention_expired'` |
| Kapsam dışı | `intraday_scope` kapısı, ağa çıkılmaz | `ItemStatus.OUT_OF_SCOPE` (§6.5c) |
| `YFPricesMissingError`, mesaj **pencere reddi** kalıbında | mesaj metni: *"data not available for startTime"* + *"must be within the last N days"* / *"Only N days ... per request"* | `empty` **değil** `failed` + `bar_gaps.reason='retention_expired'` — planlayıcı hatası demektir, görünür olmalı |
| `YFPricesMissingError`, diğer | sembol o aralıkta işlem görmemiş | `ItemStatus.empty` |
| Ağ/HTTP hatası | `classify_error` (mevcut) | `ItemStatus.failed` + `bar_gaps.reason='fetch_failed'` |

**Mesaj metnine dayanan sınıflandırma kırılgandır ve bilinçli olarak
kabul edilmiştir.** Proje bu deseni zaten kullanıyor (AH §8.4: HTTP 404 →
`empty`). Alternatif — her boş sonucu `failed` saymak — 5.000 sembollük
bir koşuda gerçek arızaları gürültüde boğardı. Kalıp `errors.py`'de tek
bir yerde tanımlanır ve yfinance sürüm yükseltmelerinde ilk kontrol
edilecek yerlerden biri olarak işaretlenir.

Bunun bir yan sonucu: **planlayıcı doğru çalışıyorsa pencere reddi
istisnası HİÇ görülmemelidir.** Görülmesi, `BAR_LIMITS`'in Yahoo'nun
güncel davranışından saptığının erken uyarısıdır.

### 8.3 Normalizasyon kuralları (yeni)

1. **`close` NULL ise satır atılır.** `price_history` ile aynı: kapanışsız
   bar anlamsızdır.
2. **`volume < 0` → NULL.** `price_history` ile aynı.
3. **Kolon seti sembole göre DEĞİŞİR.** ETF'lerde `Capital Gains` gelir
   (§4.1/3). Sabit sıraya veya varlığa güvenilmez (S §8.3) — ama bu üç
   kolon `price_bars`'a zaten yazılmaz (K3), yalnız yok sayılır.
4. **`bar_interval` `BAR_INTERVALS`'e karşı doğrulanır** (K5'in bedeli).
5. **`local_date` barın YEREL tarihidir**, `ts_utc`'den türetilmez —
   `ts_utc.date()` pozitif ofsetli borsalarda yanlış gün verir (S §5.4'ün
   aynı dersi). Kaynak index zaten yerel tz taşır (§4.1/1); `local_date`
   ondan alınır.
6. **`ts_utc` UTC'ye çevrilir ve tz-naive yazılır.** Kolon `DATETIME(6)`;
   MySQL tz taşımaz, bu yüzden çevrimin yapıldığı tek yer normalize'dır.

### 8.4 Boşluk denetimi

`bar_gaps` iki kaynaktan dolar:

- **Planlayıcıdan** (§6.2/4): kaçırılan pencere, ağa çıkmadan hesaplanır.
  `reason='retention_expired'`, `resolved_at` daima NULL. Bu satır bir
  kayıp KAYDIDIR, bir görev değildir.
- **Runner'dan**: `fetch_failed` durumunda denenmiş aralık.
  `resolved_at IS NULL` olduğu sürece bu satır bir GÖREVDİR —
  planlayıcı onu her koşuda yeniden dilimler (§6.2/5). Pencere hâlâ
  açıkken çekim başarılı olursa `resolved_at` dolar; pencere bu arada
  kapanırsa satır `retention_expired`'a çevrilir.

Bu geri besleme olmadan `bar_gaps` yalnızca bir mezar taşı olurdu:
ortadaki bir dilim düşüp sonrakiler yazıldığında watermark boşluğun
ötesine geçer ve o pencere bir daha hiç istenmezdi.

`price_bars`'a yazılan `bar_gaps` satırları dataset'in `produces`
listesindedir (§6.1) ve `normalize` çıktısında ikinci bir `TableWrite`
olarak döner. `fetch_failed` kaynaklı satırı ise runner yazar — dataset
o noktada zaten istisna fırlatmış durumdadır.

**`keepna` ile boşluk tespiti YAPILMAZ** — ölçümde fark üretmedi
(§4.2 son satır) ve piyasa tatilini gerçek boşluktan ayırt edemez.
Boşluk bilgisinin tek doğruluk kaynağı planlayıcıdır.

### 8.5 Reconciliation

Koşu sonunda, mevcut mekanizmaya ek olarak:

- `price_bars`'a yazılan satır sayısı, dataset'in bildirdiği
  `rows_verified` ile eşleşmeli (S §8.6, `apply_write` doğrulaması).
- `1m` için iki ayrı eşitlik: kapsam İÇİ sembol sayısı
  (`intraday_scope`'ta `enabled=1`) = `ok + empty + failed`; kapsam DIŞI
  sayısı (evren − kapsam) = `out_of_scope`. İkisinin toplamı evrene
  eşit olmalı. Eşleşmiyorsa bir sembol denetim kaydı olmadan atlanmış
  demektir.

### 8.6 Transaction ve eşzamanlılık

- Yazım ana thread'dedir (S §8.7); worker'lar yalnız fetch + normalize.
- `rescale`, sembolün KENDİ yazma transaction'ının İÇİNDE ve `bars_*`
  yazımından önce koşar (§6.6). İlk taslak "kendi transaction'ında"
  diyordu; bu, `_persist_symbol`'ün *"sembol başına TEK transaction: ya
  bütün olarak yazılır ya hiç"* invariant'ını ve
  `_persist_with_retry`'ın kilit çakışmasında bloğu yeniden çalıştırma
  davranışını bozardı.
- Shard'lar disjoint sembol kümeleri işler (P §4); `price_bars` ve
  `bar_rescales` üzerinde shard'lar arası satır çakışması **olmaz** —
  `bar_rescales` PK'sı `(symbol, split_date)` olduğu için iki shard aynı
  satırı yazamaz.
- Bu yüzden ek bir kilide GEREK YOKTUR. İlk taslak
  `SELECT ... FOR UPDATE` öneriyordu; ölçüm bunun hem gereksiz hem
  ETKİSİZ olduğunu gösterdi: var olmayan bir PK üzerindeki `FOR UPDATE`
  yalnız gap lock alır, gap lock'lar uyumludur, iki oturum da ilerler ve
  çakışma INSERT anında `ERROR 1213 (deadlock)` olur. Aynı sembolün elle
  iki kez koşturulması ihtimaline karşı koruma, §6.6/8'deki
  `INSERT ... ON DUPLICATE KEY UPDATE` + `ROW_COUNT()` desenidir.

### 8.7 Sembol silme — FK olmadığı için elle

`price_history` `ON DELETE RESTRICT` ile korunuyor (S §5.5): tek bir
DELETE 40 yıllık geçmişi silemez. `price_bars`'ta bu koruma **DB
seviyesinde YOKTUR** (K2).

**Ve mevcut bir komut bu boşluğa düşüyor.** `yfin symbols purge`,
silinecek tabloları `symbol_scoped_tables()` ile **FK kenarlarından**
türetiyor. `price_bars` FK taşımadığı için purge onu (a) silmez ve
(b) `ERROR 1451` ile uyarmaz da — sembol gider, barlar kalır. Daha
kötüsü: `bar_rescales` FK taşıdığı için SİLİNİR. Sonuç, ölçekleme
defteri kaybolmuş öksüz barlardır; sembol yeniden eklenirse rescale
tarihsel split'leri baştan uygular (§6.6'nın tohum sorununun aynısı).

Telafi:

- `symbols`'tan silme zaten soft-delete'tir (S §8.8); politika değişmedi.
- `symbol_scoped_tables()`'a elle bir ek liste parametresi verilir ve
  `price_bars` purge yoluna AÇIKÇA eklenir; silme sırası
  `price_bars` → `bar_gaps` → `bar_rescales` olmalıdır (defter en son).
- Aylık bakım işi öksüz satırları RAPORLAR (§7.5/2), silmez.
- `yfin` CLI'ında `price_bars`'tan tek tek sembol silen başka bir komut
  YOKTUR. Silme gerekirse elle SQL yazılır — sürtünme bilinçlidir.

---

## 9. Test stratejisi

### 9.1 Unit — ağsız, DB'siz

**`tests/unit/test_bar_windows.py`** — planlayıcı, tasarımın veri kaybına
karşı ilk savunması:

| test | beklenen |
|---|---|
| `1m`, watermark yok | 4 dilim, hiçbiri 8 günü aşmıyor, toplam 30 gün |
| `1m`, watermark 2 gün önce | 1 dilim |
| `1m`, watermark 10 gün önce | **2 dilim** (naif tek dilim 8'i aşar ve istek `YFPricesMissingError` ile DÜŞER — §4.1/9) |
| `1m`, watermark 35 gün önce | çekilebilir 30 gün dilimlenir + `gap=(watermark, now-30g)` |
| `5m`, watermark yok | 1 dilim, 59 gün |
| `60m`, watermark yok | 1 dilim, 729 gün |
| `1wk`/`1mo`, watermark yok | `period="max"`, dilim yok |
| her interval, sınır tam değeri | tampon uygulanmış (60→59, 730→729) |

**`tests/unit/test_bars_normalize.py`** — gerçek fixture'larla
(`scripts/capture_fixtures.py` ile yakalanır):

- `AAPL` 5m prepost'lu (`start=09:30, end=16:00`): `is_extended`
  04:00–09:25 ve 16:00–19:55 barlarında 1, arada 0.
- **`SHEL.L` — regresyon testi.** `has_pre_post_market_data=False`
  bildirmesine RAĞMEN 16:30 ve 16:35 barları `is_extended=1` olmalı
  (`start=08:00, end=16:30`). Bu test, silinen "kapı 1"in geri
  gelmesini engeller; o kapı bu barları normal seans sayardı (§4.5/1).
- `VWCE.DE`: aynı desen, 17:30/17:35 barları 1.
- `THYAO.IS`: `start=09:30, end=18:00`, ilk bar 09:55 → TÜM barlar 0.
  (Dejenere olan `pre_*`/`post_*` kolonları kurala hiç girmez.)
- `BTC-USD`: `start=00:00, end=23:59` → hepsi 0; `local_date` UTC
  gününe eşit.
- `1wk`/`1mo` fixture'ı: `is_extended` daima 0 (§6.4 kural 1).
- `prepost=False` ile yakalanmış bir `tradingPeriods` (yalnız
  `start`/`end` kolonlu) normalize'ı düşürmemeli (§4.5/3).
- `GC=F`: 18:10 barının `local_date`'i o günün tarihi (ertesi gün DEĞİL);
  `ts_utc` doğru.
- `SPY`/`VWCE.DE`: `Capital Gains` kolonu var → yok sayılıyor, hata yok.
- `close` NULL satırı atılıyor; `volume < 0` → NULL.
- Bilinmeyen interval → `ValueError`.

**`tests/unit/test_rescale.py`** — SQL'siz, saf hesap:

- `ratio=10` → fiyat /10, hacim ×10.
- Ters split `ratio=0.1` → fiyat ×10, hacim /10.
- `ratio=1.5` (3:2) `Decimal` ile; float ile hesaplanan sonuçtan farklı
  olduğu gösterilir.
- `volume` FLOOR ile tam sayı.
- `ratio <= 0` → uygulanmaz, `bar_rescales`'e yazılmaz, hata loglanır
  (§6.6/3).
- `split_ts` sembolün tz'sinden türetilir: BIST (+03) bir split'inde
  split günü 10:00 yerel barı ölçeklenmez, önceki günün 17:00 barı
  ölçeklenir (§6.6/2).
- Hedef interval filtresi: `1wk`/`1mo` satırları UPDATE kapsamı dışında
  (§6.6/1).

### 9.2 Repo — gerçek MySQL, ağsız

**`tests/repo/test_bars_repo.py`**:

- Partition'lı tabloya yazma; `EXPLAIN` çıktısında partition pruning'in
  gerçekleştiği doğrulanır (aralık sorgusu tüm partition'ları taramamalı).
  **Bu test bugünkü fixture'la ÇALIŞMAZ:** `tests/conftest.py` şemayı
  `Base.metadata.create_all()` ile kuruyor ve Alembic'i hiç çalıştırmıyor,
  yani `price_bars` PARTITION'SIZ oluşurdu ve `v_price_bars_regular` hiç
  var olmazdı. Partition DDL'i ve view SQL'i `models/bars.py` /
  `models/views.py` içinde sabit olarak tutulur (proje bunu
  `V_ACTIONS_CREATE` için zaten yapıyor) ve conftest onları
  `create_all()` sonrası uygular. §10 adım 2 bu conftest değişikliğini
  içerir.
- `ERROR 1526`: partition aralığı dışına yazım gürültülü şekilde
  başarısız oluyor (§5.3'ün `MAXVALUE`'suz tasarım kararının testi).
- `INSERT_CHUNK` sınırında ~20.000 satırlık yazma tamamlanıyor,
  `rows_verified` doğru dönüyor ve `align_rows` dilimlemeden ÖNCE
  uygulandığı için tüm dilimler aynı kolon setini taşıyor (§6.7).
- Upsert idempotency: aynı `TableWrite` iki kez → satır sayısı sabit,
  değerler aynı.
- **Rescale idempotency:** aynı split iki kez uygulanmaya çalışılır →
  `bar_rescales` bir satır, fiyatlar bir kez bölünmüş.
- **`rescale --seed` regresyonu — en kritik repo testi.** `splits` dolu,
  `price_bars` dolu, `bar_rescales` boş bir DB'de: önce `--seed`
  çalıştırılır, sonra normal koşu yapılır → **hiçbir fiyat değişmemiş
  olmalı.** Seed adımı atlandığında aynı senaryonun arşivi bozduğu
  (fiyatların tarihsel split çarpanına bölündüğü) ayrıca gösterilir.
- **Rescale atomikliği:** UPDATE ile INSERT arasında hata enjekte edilir
  → transaction geri alınır, fiyatlar bozulmamış.
- Öksüz satır denetim sorgusu (FK yokluğunun telafisi) öksüz satırı
  buluyor.
- `intraday_scope` çözümlemesi: `1m` boş tabloda **hiçbir sembol**,
  `5m` boş tabloda **tüm evren** (§5.4 asimetrisi); `5m` için tek bir
  satır eklendiğinde diğer semboller kapsam dışına düşüyor.
- `yfin symbols purge` sonrası `price_bars`'ta öksüz satır KALMIYOR
  (§8.7 düzeltmesinin testi).
- `v_price_bars_regular` yalnız `is_extended=0` döndürüyor.

### 9.3 Live — `-m live`

- Bir US sembolü (`AAPL`) + bir BIST sembolü (`THYAO.IS`) için `bars_5m`
  uçtan uca: fetch → normalize → yaz → doğrula.
- `bars_1m` dilimleyici gerçek 8 günlük sınırda hata ALMIYOR (planlayıcı
  tamponunun sahada doğrulanması).
- `bars_1wk` `period="max"` ile çalışıyor ve satır anahtarları Pazartesi
  hizalı.

### 9.4 Regresyon — bu tasarımın bütün gerekçesi

**Mevcut `price_history` testlerinin HİÇBİRİ değişmemelidir.** K1'in
(iki tablo) tek somut faydası budur; bir tanesi bile değiştiyse tasarım
sınırı ihlal edilmiş demektir ve durup gözden geçirilmelidir.

`WatermarkReader` imza genişlemesi (§6.3) mevcut testleri kırmamalı —
`where` parametresi opsiyoneldir.

### 9.5 Statik analiz

`ruff` (E, F, I, UP, B, SIM) ve `mypy --strict` mevcut ayarlarla temiz
geçmeli. `IntervalBarDataset.name`'in instance attribute olması
`Registrable` protokolüyle mypy altında uyumludur; değilse protokol
`name: str` olarak zaten tanımlı olduğu için sorun çıkmaz.

---

## 10. Uygulama sırası ve migration

Sıra bağlayıcıdır: her adım kendinden öncekine dayanır ve tek başına
test edilebilir.

1. **`models/bars.py`** — `PriceBar`, `IntradayScope`, `BarGap`,
   `BarRescale`; `models/base.py`'ye `BarIntervalType()`.
2. **Migration** `<zaman-damgasi>_price_bars.py` (Alembic revision üretilirken
   `20260904_1310_proxy_pool_and_repair.py` ile aynı biçimde adlandırılır) —
   dört tablo + view. Partition DDL `op.execute()` ile ELLE yazılır
   (Alembic autogenerate edemez); `p_hist` + 2023-10'dan 2028-09'a aylık
   partition, `MAXVALUE` YOK (§5.3). DDL ve view SQL'i `models/bars.py` /
   `models/views.py`'de SABİT olarak tutulur; **aynı adımda
   `tests/conftest.py` bu sabitleri `create_all()` sonrası uygulayacak
   şekilde genişletilir** (§9.2). `downgrade()` dört tabloyu ve view'ı
   düşürür.
3. **`persistence.py`: `INSERT_CHUNK`** (§6.7) — bağımsız, tek başına
   test edilebilir, mevcut dataset'lere fayda sağlar.
4. **`runner.py` + `datasets/base.py`: watermark `where` parametresi**
   (§6.3) — mevcut testler yeşil kalmalı.
5. **`datasets/bars.py`: `BAR_INTERVALS`, `BAR_LIMITS`, `plan_windows`**
   — saf fonksiyon, ağsız, önce testleri yazılır (TDD).
6. **`datasets/bars.py`: `BarPayload`, `is_extended`, normalize** —
   fixture'larla, ağsız.
7. **`datasets/bars.py`: `IntervalBarDataset.fetch` + registry kayıtları
   + alias'lar.**
8. **Kapsam kapısı** (§6.5): `ScopeReader` (runner.py, `WatermarkReader`
   deseni), `SyncContext`'e ikinci provider, `DatasetOutOfScope` (nötr
   tabandan), `_worker`'da jenerik `except`'ten ÖNCE gelen dal,
   `SymbolPayload`'a üçüncü kanal, ve yeni `ItemStatus.OUT_OF_SCOPE`
   (`RunTally.cells` + `exit_code` güncellemesi).
9. **`rescale.py`** (§6.6): `_persist_symbol`'e açık kanca noktası,
   sembol transaction'ı içinde, `bars_*` yazımından önce.
9a. **`yfin rescale --seed` ÇALIŞTIRILIR** — `bars_*` ilk kez koşmadan
   ÖNCE, migration'dan sonra. Bu adım atlanırsa ilk koşu arşivi
   tarihsel split çarpanına böler (§6.6). Sıra bağlayıcıdır ve
   `yfin bars maintain` bu tohumun varlığını her ay kontrol eder:
   `splits`'te olup `bar_rescales`'te olmayan ve `price_bars`'ın en
   erken barından ESKİ bir split varsa uyarı basar.
10. **`symbol_scoped_tables()` / `yfin symbols purge` düzeltmesi**
   (§8.7): YALNIZ `price_bars` açıkça eklenir — `bar_gaps` ve
   `bar_rescales` `symbol_fk_column` taşıdığı için FK grafiğinden
   kendiliğinden türetilir.
11. **CLI:** `yfin scope`, `yfin bars maintain`, `yfin bars gaps`,
   `yfin rescale --seed`.
12. **Live testler** ve ilk dolum koşusu.

**Fixture yakalama:** `scripts/capture_fixtures.py` genişletilir — altı
interval × yedi sembol için ham çerçeve ve `tradingPeriods`. `1m`
fixture'ları 30 gün sonra yeniden yakalanamayacağı için repoya
KAYDEDİLİR.

---

## 11. Yapılandırma ve kullanım

### Yeni `.env` anahtarları

```
# `bars` ALIAS'inin --datasets ile cagrildiginda hangi intervallere
# genisleyecegi. Registry'de ALTI interval de KAYITLIDIR (K5; tek kaynak
# BAR_INTERVALS sabitidir) - bu anahtar yalnizca alias genislemesini
# daraltir, kayit silmez. `--datasets bars_1m` her zaman calisir.
# 1m burada YOKTUR cunku ayri bir takvimde kosar (PB§11) ve sembol
# kapsami intraday_scope tablosundan gelir (K6).
YF_BAR_INTERVALS=5m,15m,60m,1wk,1mo

# Seans disi barlar yazilsin mi. DIKKAT: ek bar yalniz ABD hisselerinde
# gelmez - SHEL.L ve VWCE.DE de hasPrePostMarketData=False bildirdikleri
# halde ek bar donduruyor (PB§4.5/1). Kapatmak, o barlari KALICI olarak
# kaybetmek demektir.
YF_BAR_PREPOST=true

# Artimli pencerede geriye ortusme. Ortusme idempotenttir (PB§4.1/5);
# maliyeti birkac yuz gereksiz upsert, faydasi seans sinirindaki barin
# kacmamasi.
YF_BAR_OVERLAP_DAYS=2

```

`INSERT_CHUNK` bir `.env` anahtarı DEĞİLDİR; `persistence.py`'de
`VERIFY_CHUNK` ile simetrik bir modül sabitidir (§6.7).

**`yf_history_repair` DEĞİŞMEDİ** — `1d` hattını yönetir ve `true`
kalır. `price_bars` `repair`'i hiç kullanmaz (K8), ayrı bir anahtara
gerek yoktur.

### Komutlar

```bash
# Tum bar intervalleri
yfin sync --datasets bars

# Yalniz intraday
yfin sync --datasets intraday

# Tek interval, tek sembol
yfin sync --datasets bars_1m --symbols AAPL

# Geriye donuk cekim (date_range="api" -> gercek cekim, satir eleme degil)
yfin sync --datasets bars_60m --start 2025-01-01 --end 2025-06-30

# 1m kapsami
yfin scope add AAPL MSFT NVDA --interval 1m --note "izleme listesi"
yfin scope disable NVDA --interval 1m   # enabled=0; satir SILINMEZ
yfin scope remove  NVDA --interval 1m   # satiri siler (--force ister)
yfin scope list --interval 1m

# Aylik bakim (SILME YAPMAZ)
yfin bars maintain
yfin bars maintain --dry-run

# Kacirilan pencereler
yfin bars gaps --symbol AAPL
yfin bars gaps --since 2026-08-01 --reason retention_expired
```

### Önerilen koşu takvimi

| iş | sıklık | gerekçe |
|---|---|---|
| `yfin sync --datasets intraday` | **günlük** | `intraday` = `bars_1m,5m,15m,60m`. `1m`'in istek penceresi 8 gündür; 30 günde veri KALICI kaybolur. Bu satır günlük koşmak ZORUNDADIR. |
| `yfin sync --datasets bars_1wk,bars_1mo` | haftalık | bar sınırları haftada/ayda bir kapanır |
| `yfin bars maintain` | aylık | partition ilerletme (§5.3 tuzağı) |

---

## 12. Taşınabilirlik — PostgreSQL + TimescaleDB

Hedef bellidir (K12): proje ileride PostgreSQL + TimescaleDB'ye taşınacak.
Bu bölüm, o günün işini bugünden ucuzlatmak için MySQL'e özgü her kararı
işaretler. **Şimdi soyutlama katmanı YAZILMAZ** — mevcut mimari zaten
gerekli ayrımı yapmış: `datasets/base.py` "SQLAlchemy'ye ve MySQL'e
bağımlı DEĞİLDİR", MySQL mekaniği `RowWriter` protokolünün arkasındadır.

### 12.1 Geçişte DEĞİŞEN kararlar

| MySQL'de | PostgreSQL + TimescaleDB'de | etki |
|---|---|---|
| FK YOK (K2) — partition kısıtı | **FK GERİ GELİR**: hypertable foreign key destekler | `price_bars.symbol` `symbol_fk_column()` olur; §8.7'deki elle telafi ve §7.5/2 denetimi SİLİNİR |
| `RANGE COLUMNS(ts_utc)` + elle partition ekleme | `create_hypertable(..., chunk_time_interval => INTERVAL '1 month')` | §7.5/1 (aylık `ADD PARTITION`) SİLİNİR; §5.3'ün `ERROR 1526` ile gürültülü düşme tasarımına da gerek kalmaz |
| `ON DUPLICATE KEY UPDATE` | `INSERT ... ON CONFLICT (...) DO UPDATE` | `persistence.py` içinde, protokolün arkasında |
| `GREATEST(mevcut, yeni)` monotonluğu | aynı sözdizimi çalışır | değişiklik yok |
| `DATETIME(6)` | `timestamptz` | tz'yi DB taşır; §8.3/6'daki "tz-naive yaz" kuralı gevşer |
| `VARCHAR(n) CHARACTER SET ascii COLLATE ascii_bin` | düz `text`/`varchar` | PG varsayılanı zaten case-sensitive; S §5.1'in tüm collation hileleri GEREKSİZLEŞİR |
| `LONGTEXT` (`raw_json`) | `text` | JSON tipi yine KULLANILMAZ — anahtar sırası hash için korunmalı (S §5.4) |
| `BIGINT UNSIGNED` | `bigint` (unsigned yok) | `volume` için `CHECK (volume >= 0)` |
| `FLOOR(volume * ratio)` | aynı | değişiklik yok |

### 12.2 Geçişte KAZANILANLAR (bugün tasarıma girmez, YAGNI)

- **Sıkıştırma:** Timescale kolon bazlı sıkıştırmayla time-series'te
  tipik 10-20× kazanç sağlar; ~64 GB/yıl birkaç GB'a inebilir.
- **Continuous aggregate:** `5m`/`15m`/`60m`'i `1m`'den otomatik türetme.
  Bu, K-kararlarını değiştirebilir — Yahoo'dan ayrı çekmek yerine
  türetmek. **Bugün planlanmaz**: türetilmiş barın Yahoo'nunkiyle
  uzlaştırılması ayrı bir tasarım sorusudur ve ölçülmemiştir.

### 12.3 Geçiş maliyeti

Geçiş anında `price_bars` birikmiş olacak (yılda ~464 milyon satır).
Toplu aktarım bu tasarımın kapsamı DIŞINDADIR ve kendi spec'ini
gerektirir. Bu bölümün amacı yalnızca, o spec'i yazacak kişinin
"hangi karar MySQL yüzünden böyleydi" sorusunu doküman içinde
cevaplayabilmesidir.

---

## Ek A — Ölçüm günlüğü

Tüm ölçümler 2026-09-04'te, `yfinance 1.7.0`, doğrudan bağlantı
(proxy'siz), `.venv/bin/python` ile yapıldı. Scriptler
`scratchpad/m1.py … m14.py`.

| # | Ölçüm | Sonuç | Kullanıldığı yer |
|---|---|---|---|
| m1 | `1m` pencere sınırları | 8 gün/istek, 30 gün derinlik; 9 gün → *"Only 8 days ... per request"*; `-45g..-38g` → EMPTY | §4.3, §6.2 |
| m2 | `2m/5m/15m/30m/90m/60m/1h` sınırları | 60 ve 730 tam gün REDDEDİLDİ; `30m` logu: *"(30m resampled from 15m)"* | §4.3, §1 kapsam dışı |
| m3 | Sınır daraltma | `5m` 59 gün OK, `1h` 729 gün OK, `1m` 8 gün OK | §4.3 tamponu |
| m4 | `prepost/keepna/rounding/repair/back_adjust` | prepost 312→768; rounding `319.55999755859375`→`319.56`; back_adjust `Adj Close`'u SİLDİ; `Adj Close` intraday'de VAR | §1 kapsam dışı, §4.1 |
| m5 | Çok pazarlılık, 7 sembol × 5m | 7 farklı tz; `GC=F` ilk bar 18:10; ETF'lerde 9 kolon | §4.5 |
| m6 | `download()` 1d, 3 sembol | index tz DÜŞTÜ; 8 NaN/sembol; BTC Pazar satırı diğerlerinde NaN | §4.2 |
| m7 | `download()` 1h + `1wk/1mo/3mo/5d` + DST | AAPL 126, THYAO 90 NaN; THYAO barları NY tz'sine çevrilmiş; `5d` index `06-04,06-09,06-24` | §4.2, §1, §4.7 |
| m8 | `3mo`/`1mo` repair farkı, idempotency | `1mo` repair'siz 24/`2024-10-01`, repair'li 25/`2024-09-01`; 78 ortak bar birebir eşit | §4.2, §4.1/5 |
| m9 | `tradingPeriods` yapısı | 6 kolon, fetch aralığı kadar satır; `THYAO.IS` `start==end`; `BTC-USD` 00:00–23:59; `has_pre_post` yalnız AAPL/SPY True | §4.5, §6.4 |
| m10-12 | `repair` istek maliyeti | `5m` 1→6, `15m` 1→8, `60m` 1→2; `1wk`/`1mo` `1d`'den resample | §4.4, K8 |
| m13-14 | Split ölçeklemesi (NVDA 10:1) | `2024-05-20 Open=93.75` (gerçek ~937.50); `Volume=528.402.000` (gerçek ~52,8M) | §4.6, K4, §6.6 |
| v1-v2 | `prepost` ek bar, `hasPrePostMarketData=False` sembollerde | `SHEL.L` +5 bar (16:30, 16:35), `VWCE.DE` +8 bar (17:30, 17:35); `THYAO.IS`/`BTC-USD`/`GC=F` +0 | §4.5/1, §6.4 |
| v1-v2 | `tradingPeriods` gerçek değerleri | `THYAO.IS` `start=09:30 end=18:00`, `SHEL.L` `start=08:00 end=16:30`, `AAPL` `09:30/16:00`; dejenere olan `pre_*`/`post_*` | §4.5/2, §6.4 |
| v1 | `tradingPeriods` kolon seti | `prepost=False` çekiminde `pre_start` YOK → `KeyError` | §4.5/3 |
| v3 | `hide_exceptions` etkisi | `True` → boş DataFrame; **`False` (bu projenin ayarı) → `YFPricesMissingError`** | §4.1/9, §8.2 |
| v4 | `1wk`/`1mo` istek sayısı | KARARSIZ: `KO 1wk`=1, `PEP 1wk`=3 (biri `1d`) | §4.4, §7.4 |
| canlı | `1m` derinlik sınırı | **30 gün RED, 29 gün 1950 bar** — ilk canlı koşuda yakalandı, `BAR_LIMITS` düzeltildi | §4.3, §6.2 |
| canlı | uçtan uca `bars_5m` + `bars_1m` | AAPL 8.256 + 20.095 bar, THYAO.IS 4.068 bar; ikinci koşu 869 satır çekti ve toplam DEĞİŞMEDİ (idempotent) | §7.3 |
| MySQL | `ERROR 1506` partition+FK, `ERROR 1503` unique key, `ERROR 1526` aralık dışı, `ERROR 1493` `MAXVALUE`+`ADD PARTITION`, gap-lock deadlock, `DECIMAL` bölme kaybı | canlı sunucuda doğrulandı | K2, §5.3, §6.6, §8.6 |

### Ek A.1 — İnceleme sırasında düzeltilen tasarım hataları

1. **`download()` ile toplu çekim önerildi ve ölçümle geri alındı.**
   İlk kapsam sorusunda "istek sayısını düşürür" varsayımıyla sunuldu;
   `multi.py` okununca sembol başına ayrı `history()` çağırdığı, kendi
   thread havuzunu açtığı ve çok pazarlı evrende uydurma satır ürettiği
   görüldü. Ders: yfinance'in "toplu" görünen API'si toplu değildir.
2. **`2m` interval'i kümede tutuldu, sonra çıkarıldı.** Hacim
   projeksiyonu yapılınca (605 milyon satır/yıl) `1m` arşivi varken
   taşıdığı ek bilginin sıfır olduğu görüldü.
3. **`repair`'in intraday'de açık kalması planlandı, ölçümle kapatıldı.**
   Maliyetin CPU değil AĞ olduğu (`5m` 6×, `15m` 8×) ölçülene kadar
   fark edilmedi.
4. **`adj_close` kolonu şemada vardı, çıkarıldı.** Kalıcı arşivde
   bayatlayacağı fark edildi.
5. **Split ölçeklemesi tasarımın SONUNDA fark edildi.** `auto_adjust=False`'ın
   ham fiyat verdiği varsayılıyordu; NVDA ölçümü bunu çürüttü ve §6.6
   (rescale) tasarıma eklendi. Bu bulgu olmasaydı arşiv, ilk split'te
   sessizce bozulacaktı.

### Ek A.3 — Uygulama ve uygulama sonrası denetimde bulunanlar

Kod yazıldıktan sonra "eksik kalan var mı" denetimi yapıldı. Aşağıdakiler
spec'te tanımlıydı ama koda geçmemişti; hepsi kapatıldı ve testleri
yazıldı.

| Bulgu | Neden sessizdi |
|---|---|
| **`fetch_failed` boşluk yazımı hiç yoktu** | Sabit yalnız okuma tarafındaydı; planlayıcının `open_gaps` mekanizması hiç dolmayan bir tablodan okuyordu — **ölü koddu** |
| **`resolved_at` hiç doldurulmuyordu** | Bir kez yazılan boşluk sonsuza kadar açık kalır, her koşuda boşuna yeniden çekilirdi |
| **Dilim bazlı hata izolasyonu (§8.1) uygulanmamıştı** | Tek dilim düşünce tüm fetch düşüyordu: `1m` ilk dolumunda bir ağ hatası 29 günün tamamını kaybettirirdi |
| **`normalize_bars` boş çerçevede erken dönüyordu** | Tam da boşluk kaydedilmesi gereken durumda (dilim düştü, veri yok) onu yutuyordu |
| `RunSummary.cells` `OUT_OF_SCOPE`'u dışlamıyordu | İki çıkış kodu yolu (`RunTally` / `RunSummary`) ayrışmıştı |
| `yf_bar_intervals` hiçbir yerde okunmuyordu | Ölü konfigürasyon → **kaldırıldı** (YAGNI) |
| `history_metadata`'da IANA tz adı yokmuş | Mevcut `timezone` kolonu `"EDT"`/`"TRT"` taşıyor; rescale'in ihtiyacı olan `exchangeTimezoneName` hiç şemaya alınmamıştı → yeni kolon + migration |
| **`1m` derinliği 30 değil 29** | Tam sınır hiç ölçülmemişti; ilk canlı koşuda `YFPricesMissingError` ile yakalandı |
| **Rescale yalnız `--seed`'e yaslanıyordu** | Operasyonel bir adımın unutulması geri alınamaz bozulma üretirdi → yapısal ikinci kapı (§6.6) |

### Ek A.2 — Bağımsız inceleme turunda düzeltilen hatalar

Doküman yazıldıktan sonra üç bağımsız inceleme yapıldı (iç tutarlılık,
kod uyumu, teknik doğrulama). Aşağıdakiler ÖLÇÜMLE ya da KODLA
kanıtlanmış hatalardı; hepsi düzeltildi.

**Tasarımı bozacak olanlar:**

1. **`rescale` ilk koşuda arşivi yok ediyordu.** Tetikleyici
   ("`bar_rescales`'te karşılığı olmayan her split") ile kapsam sınırı
   iddiası ("`bar_rescales` boş başlar, yalnız yeni split'lerle dolar")
   birbirini çürütüyordu: `splits` tablosu mevcut hat tarafından zaten
   dolu olduğu için ilk koşu AAPL arşivini 224'e bölerdi. → `rescale
   --seed` tohumlama adımı (§6.6, §10/9a).
2. **`is_extended`'in 1. kapısı yanlıştı.** `has_pre_post_market_data`
   güvenilmez: `SHEL.L` ve `VWCE.DE` `False` bildirip ek bar
   döndürüyor. O kapı, seans dışı barları `v_price_bars_regular`'a
   sokardı — view'ın önlemek için var olduğu bozulmanın ta kendisi.
   → Kapı silindi, kural yalnız `tradingPeriods.start/end`'e dayanıyor.
3. **`empty` ≠ `failed` tablosu yanlış öncüle dayanıyordu.** "Pencere
   reddi exception fırlatmaz" ölçümü, projenin `hide_exceptions=False`
   ayarı olmadan yapılmıştı. → §8.2 yeniden yazıldı, mesaj kalıbına
   dayalı sınıflandırma eklendi.
4. **`depends_on = ("symbols", "splits")` iki kez yanlıştı.** `splits`
   bir tablo adı (registry dataset adıyla çözümler), ve dataset adı
   olsaydı bile `fetch_history_frame` üzerinden tam bir 1d çağrısı
   getirirdi. → `("symbols",)`; rescale `splits` TABLOSUNU okur.
5. **`not_attempted` her koşuyu `partial` yapardı.** `exit_code`
   invariant'ı: "işlenmemiş sembol varken çıkış kodu asla 0 olamaz".
   → Yeni `ItemStatus.OUT_OF_SCOPE`.
6. **Başarısız dilim bir daha denenmiyordu.** `bar_gaps` bir mezar
   taşıydı; watermark boşluğun ötesine geçince pencere kapanmadan
   kaybediliyordu. → `resolved_at` kolonu + planlayıcıya `open_gaps`.
7. **`MAXVALUE` partition'ı zararlıydı.** Ölçüldü: `MAXVALUE` varken
   `ADD PARTITION` imkânsız (`ERROR 1493`), zorunlu `REORGANIZE` tüm
   satırları kopyalar; `MAXVALUE`'suz tabloda aralık dışı insert
   `ERROR 1526` ile gürültülü düşer. → `MAXVALUE` kaldırıldı.
8. **`SELECT ... FOR UPDATE` korumazdı.** Ölçüldü: var olmayan PK'da
   yalnız gap lock alınır, gap lock'lar uyumludur, iki oturum da ilerler
   ve INSERT'te `ERROR 1213` olur. → `INSERT ... ON DUPLICATE KEY
   UPDATE` + `ROW_COUNT()`.
9. **`yfin symbols purge` `price_bars`'ı atlayıp `bar_rescales`'i
   siliyordu** (FK türetmesi nedeniyle). → §8.7 ve §10/10.

**Uygulanamaz yazılmış olanlar:** `SyncContext`'in DB erişimi yok
(`ScopeReader` eklendi); `DatasetOutOfScope` mevcut akışta `FAILED`
olurdu; `INSERT_CHUNK` `align_rows`'u bozardı; test conftest'i Alembic
çalıştırmadığı için partition/view testleri imkânsızdı.

**Aritmetik ve ölçüm hataları:** `DECIMAL(28,12)` 14 bayttır (13 değil)
→ satır 107 B; `adj_close` kazancı 6,5 GB (20 değil); `1m` tüm evren
129 GB (150 değil); `local_date` indeksinin ~14 GB'ı projeksiyona
eklendi; `1wk`/`1mo` istek sayısı kararsız çıktı → bütçe üst sınıra
çevrildi (~41.000/gün); `repair` ek maliyeti tek bir sembol sayısına
oturtuldu (+60.000); `repair` 6×/8× sabit değil, gözlenen en kötü
durum; yedi yanlış kod satırı referansı düzeltildi.
