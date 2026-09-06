# yfinance Search & Lookup + Screener & Query → MySQL — Tasarım Dokümanı

Kısaltma: bu dokümana yapılan atıflar **SQ** önekiyle verilir (SQ §5.3 gibi).
Diğer tasarımlara atıflar mevcut önekleriyle: S (ETL), AH (Analysis &
Holdings), PB (Price Bars), P (Proxy), SI (Sector & Industry).

Bu doküman üç bağımsız denetim turundan geçmiştir (iç tutarlılık, kod uyumu,
canlı API yeniden ölçümü). Denetimde çürütülen kararlar ve düzeltilen ölçümler
Ek A.1'de kayıtlıdır — aralarında tasarımın ilk hâlindeki **çekirdek maliyet
kararı** (K6) da vardır.

---

## 0. Önkoşullar

Bu tasarım aşağıdakilerin **uygulanmış** olduğunu varsayar ve doğrulanmıştır:

| Önkoşul | Nereden gelir | Bu tasarımda ne için gerekli |
|---|---|---|
| `Dataset` / `SyncContext` sözleşmesi | S §6.1 | `search` ve `lookup` sembol ekseninde birer dataset |
| `GlobalDataset` / `MarketContext` | S §6.4 | `screener` piyasa ekseninde |
| `HashGatedDataset` | S §6.3/b | `screen_runs → screen_members` kapısı |
| `AsOfGate` mixin'i + `asof_gate_table` uzatma noktası | SI §6.2 | `search`/`lookup` kendi kapı tablosunu bildirir |
| `runner.py` shard + kuyruk + proxy | P §4 | 4.500 terimlik sembol döngüsü |
| `market_runner.py` bölge döngüsü | S §7.2 | Ekran döngüsünün taşınacağı yer |
| `models/fields.py` `Field` tablosu | S §6.6 | `SCREENER_QUOTE_FIELDS` |
| `prune_asof(registry, gate_table, scope_column)` | SI §11.2 | Yeni kapı tablosunun budanması |
| **SI dataset katmanı — TAMAMI** | SI §7 | `research_reports` yeniden adlandırmasının etki alanı |
| `news` + `news_symbols` | S §5.2, §5.5 | Search haber bloğunun hedefi |

**SI katmanı tamamen uygulanmıştır** ve üretimde koşmaktadır:
`datasets/domain/{taxonomy,profile,rankings}.py` beş dataset kaydeder
(`taxonomy.py:184`, `rankings.py:262-263`, `profile.py:328-329`),
`domain_runner.py` mevcuttur (`yfin_domain_sync` kilidi),
`datasets/domain/__init__.py` üçünü de import eder.

Bunun bu tasarım için **bağlayıcı sonucu** vardır: §5.3'teki tablo yeniden
adlandırması, çalışan beş dataset'in yazdığı bir tabloya dokunur ve
§12.1'deki migration ile **aynı** değişiklik kümesinde kod tarafını da
güncellemek zorundadır. Etki alanı §5.3'te dosya dosya listelenmiştir.

---

## 1. Amaç

Yahoo Finance'in üç keşif ucunu — `Search`, `Lookup`, `screen` — MySQL'e
eksiksiz ve tiplenmiş biçimde yazmak. İki ayrı değer üretilir ve **ikisi de
istenmiştir**:

1. **Keşif.** Bu üç uç, evrende olmayan sembolleri getirir. Tek bir custom
   sorgu (`region=tr`) 628 BIST hissesini üç istekte bildiriyor
   (SQ §4.1/14, §4.4). Keşfedilen her sembol `symbols` tablosuna **pasif**
   yazılır.
2. **As-of arşiv.** "AAPL 2026-09-04'te `day_gainers`'ta kaçıncı sıradaydı",
   "THYAO.IS aramasında hangi semboller birlikte döndü", "bu ekranın kadrosu
   dün neydi" sorguları tarih bazında yanıtlanabilir olur.

### Kapsam içi

- `yf.Search` → `quotes`, `news`, `researchReports`, `lists` blokları
- `yf.Lookup` → belgeler + `lookupTotals`, **adaptif çağrı stratejisiyle** (K6)
- `yf.screen` → 19 predefined ekran + kodda tanımlı custom ekranlar
- `EquityQuery` / `FundQuery` / `ETFQuery` ile custom sorgu tanımı
- Keşfedilen sembollerin `symbols`'a pasif terfisi ve elle aktifleştirme
- İlişkilerin **sembol kodu** üzerinden kurulması

### Kapsam dışı — gerekçeli, tahminle değil ölçümle

- **`Search.nav` bloğu.** Ölçülen tek şekil: `{"navName": "Airlines",
  "navUrl": "https://finance.yahoo.com/sectors/industrials/airlines"}`. İki
  alan, ikisi de UI navigasyon bağlantısı; finansal veri değil, sembol
  ilişkisi yok.
- **`lists` üyelik satırları.** `lists` bloğunun satırları sembol
  **taşımaz** (§4.1/6). Ama "yalnız sayı var" da doğru değildir: satır
  üyeliği çözecek **kimliği** taşır (`ALGO_WATCHLIST` şeklinde `pfId` +
  `userId`, `PREDEFINED_SCREENER` şeklinde `canonicalName`). Üyeliği çekmek
  **ikinci bir istek** gerektirir ve `yfinance` sarmalayıcısında karşılığı
  yoktur. Kapsam dışı bırakma gerekçesi **maliyet ve K7**, veri yokluğu
  değildir. Bu ayrım önemlidir: gelecekte istenirse veri oradadır.
- **Lookup sayfalama.** `Lookup._fetch_lookup` `"start": 0` değerini **sabit**
  gönderiyor (`lookup.py:67`). Sayfalama sarmalayıcıyı atlamayı gerektirir
  (K7). Bedeli §4.1/8'deki tavandır ve `lookup_totals` tablosu bu kaybı
  **kayıt altına alır** (§9.6).
- **Predefined ekran listesinin dinamik keşfi.** "Ekranları listele" diye bir
  uç yok; `PREDEFINED_SCREENER_QUERIES` kütüphane sabitidir. Kendi
  `screens.py` sabitimiz kaynak alınır. **Not:** `Search.lists` bloğu, bu 19
  anahtarın dışında düzinelerce predefined ekran adı bildiriyor
  (`MS_TECHNOLOGY`, `SEMICONDUCTORS`, `HIGHEST_DIVIDEND_STOCKS`, …) —
  §8.3'te keşif sinyali olarak kaydedilir ama otomatik koşturulmaz.
- **`screenerFieldResults` bloğu.** Ölçülen her sorguda boş liste.
- **WebSocket, Authentication.** Ayrı tasarımlar.

---

## 2. Kararlar ve gerekçeleri

| # | Karar | Gerekçe |
|---|---|---|
| **K1** | Dördüncü eksen **yaratılmaz** | `search`/`lookup` sembol ekseninde (`ctx.symbol` = sorgu terimi), `screener` piyasa ekseninde. Yeni bir `QueryContext` + `QUERY_DATASETS` + `query_runner` ~400 satır makine demekti. Kuyruk, shard, proxy rotasyonu, `sync_run_items` sembol kırılımı bedavaya gelir. |
| **K2** | `GlobalDataset.scope`'a üçüncü değer: `"variant"` | Ekran döngüsü dataset'in **dışında** döner. Bölge döngüsünün `market/base.py` başlığındaki gerekçesi kelimesi kelimesine geçerli: `sync_run_items` granülerliği doğal olarak (dataset × ekran × tablo) olur. |
| **K3** | Yeni hash kapısı **sınıfı** yok, yeni kapı **tablosu** var | `search`/`lookup` mevcut `AsOfGate` mixin'ini kullanır ama kendi tablosunu bildirir (`discovery_asof_state`). Bu, SI'nin `domain_asof_state` için kullandığı uzatma noktasının aynısıdır (`domain/base.py:146-147`). Gerekçe K3a'da. |
| **K3a** | `asof_state` **kullanılamaz** | `asof_state.symbol` `symbols.symbol`'a **FK taşır** (`models/asof.py:33` → `symbol_fk_column`, `ondelete=RESTRICT`). Serbest terim (`"Turkish Airlines"`) `symbols`'ta yoktur → `ERROR 1452`. Uzunluk sınırı bu sorunu çözmez. SI aynı duvara çarpıp `domain_asof_state`'i açmıştır. |
| **K4** | `screen_runs.content_hash` **yalnız kadroyu** kapsar | Kotasyon metrikleri gövdeye girseydi fiyat her gün oynadığı için hash **hiçbir zaman** eşitlenmez, mekanizma sessizce ölürdü — `VOLATILE_COLUMNS`'un `asof_base.py:65`'te çözdüğü sorunun aynısı. |
| **K5** | `screen_quotes` **ekrandan bağımsız**, PK `(symbol, as_of_date)` | Beş ekranda birden görünen sembolün 98 alanı beş kez yazılmaz. Kapının silme kapsamına girmemesi **zorunludur**: bir sembolün kotasyonu tek bir ekranın malı değildir. |
| **K6** | Lookup çağrısı **adaptiftir**: önce `all`, gerekirse yedi tip | **Denetimde çürütülen ilk karar.** İlk hâli "her zaman tek `all` çağrısı" idi ve tek bir dar terimle (`BTC`) ölçülmüştü. Geniş terimlerde `all` ~1.000 belgede sertçe kırpılıyor ve tipli birleşim **kat kat** geniş: `GOLD` → birleşim 3.313, `all` 996, fark **iki yönde** (354 / 2.671). Ölçüm §4.1/8. |
| **K7** | Yalnızca sarmalayıcı üzerinden çağrı | Ham HTTP çağrısı proxy yapılandırmasını (P §6.1), tz/cookie önbelleğini ve hata sınıflandırmasını atlardı. `Lookup._fetch_lookup` sarmalayıcının **kendi** metodudur; kullanmak K7'yi çiğnemez. |
| **K8** | `domain_research_reports` → `research_reports` | Rapor kimlikleri **tek uzay**: `ARGUS_48138_TechnicalAnalysis_1788520901000` (domain) ile `ARGUS_2660_AnalystReport_1785496444000` (search) aynı sağlayıcı, aynı biçim. İki tablo aynı raporu iki kez, farklı kolon altkümeleriyle tutardı. Etki alanı §5.3'te; SI katmanı canlı olduğu için migration ve kod **aynı** değişiklikte gider. |
| **K9** | Sembol kolonlarında **FK yok** | `news_symbols`'ün gerekçesi (`models/news.py:45-51`) burada daha keskin: keşif dataset'leri **tanımı gereği** evren dışı sembol döndürür. FK olsaydı `symbols` yazımı düştüğünde o hücrenin tüm verisi rollback olurdu. |
| **K10** | Keşif yazımı `is_active`'i **güncellemez** | Elle aktifleştirilmiş sembol ertesi gün aynı ekranda görüldüğünde sessizce pasife dönerdi. `first_seen_at`'in AH §5.4'te kurduğu "yalnız INSERT'te yazılır" kuralının aynısı. |
| **K11** | Keşif alt sistemi ayarla açılır | `Registry.resolve(None)` kayıtlı **her** dataset'i döndürür (`registry.py:113`). Koşulsuz kayıt, çıplak `yfin sync`'e sembol başına iki istek eklerdi: 4.500 sembolde **+9.000 istek/gün**. `sustainability` deseni (`analysis/sustainability.py:53`). Bayrağın **tüm** alt sistemi kapattığı §13.2'de açıkça yazılıdır. |
| **K12** | Sayfalamada ilk sayfa `count`, sonrakiler `size` | Ölçüm: `offset` verildiğinde `count` **sessizce yok sayılıyor** (§4.2/3). Tek bir parametre adıyla yazılsaydı sayfa başına 250 yerine 25 satır çekilir ve **hata verilmezdi**. |
| **K13** | Geçersiz ekran adının 404'ü `failed`'dir | Proje "HTTP 404 → `empty`" kuralını taşıyor (AH §8.4). Burada 404 "veri yok" değil **yapılandırma hatası**dır. SI §8.2'deki kardeşi. |
| **K14** | Sembolsüz `quotes` satırları **elenir** | `Search(include_cb=True)` (varsayılan) Crunchbase özel-şirket kayıtları döndürüyor: `{index, name, permalink, isYahooFinance}` — `symbol` **yok** (§4.1/3). `yfinance`'in `.quotes` özelliği bunları zaten süzüyor (`search.py:110`); `.response` ham gövdesi süzmüyor. |
| **K15** | Sayfalamada sıra **açıkça** verilir | `yf.screen`'de `sortAsc` varsayılanı `None` → azalan. Sayfalar arası sıra kararlı olmazsa sayfalar örtüşür ya da sembol atlanır. Her `ScreenDef` `sort_field` + `sort_asc` bildirir. |

---

## 3. Ortam

| Bileşen | Sürüm / değer |
|---|---|
| `yfinance` | 1.7.0 |
| Python | 3.13 |
| MySQL | 8.3, `utf8mb4_0900_ai_ci` (sembol kolonları `ascii_bin`) |
| Ölçüm tarihi | 2026-09-04 (ilk tur + bağımsız doğrulama turu) |
| Search çağrı imzası | `Search(q, max_results=10, news_count=5, lists_count=10, include_research=True, include_nav_links=True)` — **varsayılan değildir**, §4.1/5 |
| Ölçüm sembolleri | AAPL, THYAO.IS, BTC-USD, SPY, ^GSPC, VWCE.DE, GC=F |
| Ölçüm terimleri | BTC, AAPL, GOLD, TECH, THYAO, "Turkish Airlines", zzzqqxnope |
| Ölçüm ekranları | 19 predefined + `region=tr` custom |
| Ölçülen kotasyon satırı | 150 (ilk tur) + 150 (doğrulama turu) |
| Ölçülen benzersiz sembol | 1.967 (ilk tur) + 9.243 (doğrulama turu) |

---

## 4. API keşif bulguları

### 4.1 Doğrulanmış davranışlar

1. **`Search` beş blok döndürür**, `yfinance` bunları `quotes` / `news` /
   `lists` / `research` / `nav` özelliklerine ayırır. Yanıt 20 üst düzey
   anahtar taşır; kalanı zamanlama telemetrisi ve `explains` /
   `screenerFieldResults`.

2. **`quotes` alan seti kotasyon tipine göre VE aynı tip içinde değişir.**
   Ölçüldü:

   | Sorgu | quote | quoteType | anahtar (min–max) |
   |---|---:|---|---|
   | `AAPL` | 7 | EQUITY, ETF (+ sembolsüz) | 4 – 15 |
   | `THYAO.IS` | 1 | EQUITY | 14 |
   | `BTC-USD` | 7 | CRYPTOCURRENCY | 10 |
   | `SPY` | 7 | ETF (+ sembolsüz) | 4 – 10 |
   | `^GSPC` | 1 | INDEX | 10 |
   | `VWCE.DE` | 1 | ETF | 10 |
   | `GC=F` | 6 | EQUITY, FUTURE, MUTUALFUND (+ sembolsüz) | 4 – 16 |

   **Aynı `quoteType` içinde bile** alan sayısı değişiyor: `GC=F`'de iki ayrı
   EQUITY satırı 12 ve 16 anahtar taşıdı. `sector`/`industry` aileleri
   yalnız EQUITY'de; `prevName`/`nameChangeDate` seyrek. **Eksik alan hata
   değildir**; tüm kolonlar nullable.

3. **`quotes` bloğu sembolsüz satır taşır.** `include_cb=True` (varsayılan)
   iken Crunchbase özel-şirket kayıtları geliyor:
   `{'index': '78ddc076…', 'name': 'AAPlasma', 'permalink': 'aaplasma',
   'isYahooFinance': False}` — 4 anahtar, `symbol` **yok**, `quoteType`
   **yok**. Ölçülen her serbest terim sorgusunda en az bir tane çıktı.
   `yfinance`'in `.quotes` özelliği bunları süzer (`search.py:110`:
   `if "symbol" in quote`), `.response` süzmez. K14.

4. **Search sorgulanandan başka sembol döndürür.** `AAPL` sorgusu ETF'ler
   dahil 6 sembollü kotasyon getirdi. Keşif vektörü budur.

5. **`researchReports` ve `nav` varsayılan olarak KAPALIDIR.**
   `search.py:32-34` imzası: `include_research=False`,
   `include_nav_links=False`. Varsayılan çağrıda `"Turkish Airlines"` →
   quotes 0, news 8, **research 0, nav 0**. `include_research=True,
   include_nav_links=True` ile → quotes 0, news 8, **research 3, nav 1**.
   Bu bayraklar açık verilmezse `research_reports` ve `search_report_hits`
   **hiç satır almaz** ve §9.6 eksiksizlik kanıtı bunu yakalamaz.

6. **`lists` bloğu İKİ ŞEKİLLİDİR.** 10 terimde 41 satır ölçüldü:

   | `type` | adet | anahtarlar |
   |---|---:|---|
   | `ALGO_WATCHLIST` | 23 | `index`, `score`, `type`, `iconUrl`, `slug`, `name`, `brandSlug`, `pfId`, `symbolCount`, `dailyPercentGain`, `followerCount`, `userId` (12) |
   | `PREDEFINED_SCREENER` | 18 | `index`, `score`, `type`, `iconUrl`, `id`, `title`, `canonicalName`, `total`, `isPremium` (9) |

   Satırın kendisi **sembol taşımaz**, ama üyeliği çözecek **kimliği** taşır:
   `ALGO_WATCHLIST` → `pfId` + `userId`, `PREDEFINED_SCREENER` →
   `canonicalName` (`GOLD`, `MS_TECHNOLOGY`, `SEMICONDUCTORS`,
   `HIGHEST_DIVIDEND_STOCKS`, …). `symbolCount` yalnız birinci şekilde,
   `total` yalnız ikincide.

7. **Search haberi `Ticker.news` ile aynı kimlik uzayındadır.** `AAPL` için
   `Ticker.news` id kümesi ile `Search.news` uuid kümesi kesişiyor
   (`fa6a42d9-7022-3d2c-856e-c36016352b47`). Aynı `news_id` PK'sı kullanılır.

8. **Lookup: `all` çağrısı ~1.000 belgede kırpılır; geniş terimde tipli
   birleşim KAT KAT geniştir.** Dört terimde, her tip `count=1000`:

   | Terim | tipli birleşim | `all` belge | `all` − birleşim | birleşim − `all` | `lookupTotals.all` |
   |---|---:|---:|---:|---:|---:|
   | AAPL | 57 | 57 | 0 | 0 | 57 |
   | BTC | 500 | 500 | 0 | 0 | 503 |
   | **GOLD** | **3.313** | **996** | **354** | **2.671** | 7.261 |
   | **TECH** | **4.024** | **998** | 0 | **3.026** | 10.000 |

   Dar terimlerde (`lookupTotals.all ≲ 500`) `all` yeterlidir ve fark iki
   yönde sıfırdır. Geniş terimlerde `all` kaybın **%70'inden fazlasını**
   üretir ve `GOLD`'da fark **iki yönlüdür** — 354 sembol (hepsi `0P…` fon
   kodları) yalnız `all`'da, 2.671 sembol yalnız tipli çağrılarda. K6 bu
   ölçümle **adaptif** hâle getirildi.

   Sembol döngüsünde bu neredeyse hiç tetiklenmez (AAPL 57, THYAO 1); asıl
   etkisi serbest terimlerdedir (`lithium`, `tech`, `gold`).

9. **Sekiz tipin kolon seti ÖZDEŞ DEĞİL, üst küme olarak ortaktır.** Dört
   terimde ölçüldü: 13 anahtarın 11'i her tipte var;
   `industryLink` / `industryName` **yalnız `equity`** (ve dolayısıyla `all`)
   belgelerinde; `shortName` bazı `index` ve `mutualfund` satırlarında yok.
   Ortak çekirdek: `symbol`, `exchange`, `quoteType`, `rank`, `fulldayPrice`,
   `fulldayChange`, `fulldayChangePercent`, `regularMarketPrice`,
   `regularMarketChange`, `regularMarketPercentChange` (+ `shortName`
   çoğunlukla).

10. **`lookupTotals` dokuz tip bildirir.** `BTC` ölçümü:
    `{all: 503, equity: 56, mutualfund: 19, etf: 99, index: 2, currency: 0,
    future: 5, cryptocurrency: 322, privateCompany: 0}`. **`privateCompany`
    `LOOKUP_TYPES` sabitinde yoktur** (`lookup.py:31`). Altı özel-şirket adı
    sorgusunda (`OpenAI`, `Stripe`, `SpaceX`, `Anthropic`, `Databricks`,
    `Shein`) değeri **hep 0** döndü; sıfırdan farklı örnek bulunamadı. Yine de
    yanıttan okunur, sabitten değil.

11. **`total` ≠ `lookupTotals.all` genel bir olgudur.** `SpaceX`: `total=22`,
    `lookupTotals.all=72`. Yalnızca 500/503 tavanı değil, dar terimlerde de
    ayrışıyor. `lookup_totals` tablosu bu farkı kaydeder (§9.6).

12. **Screener sayfalaması `total`'a kadar çalışır.** `top_mutual_funds`
    `total=1788`: `offset=250, size=250` → 250 satır; `offset=1750` → 38
    satır (1750 + 38 = 1788 ✓); `offset=9000` → 0 satır, **hata yok**.

13. **İlk sayfa (predefined GET) 17 anahtar, sonraki sayfalar (POST) 5.**
    GET: `canonicalName`, `count`, `creationDate`, `criteriaMeta`,
    `description`, `iconUrl`, `id`, `isPremium`, `lastUpdated`,
    `predefinedScr`, `quotes`, `rawCriteria`, `start`, `title`, `total`,
    `useRecords`, `versionId`. POST: `count`, `quotes`, `start`, `total`,
    `useRecords`. **Custom ekranın ilk sayfası da POST'tur** ve 5 anahtar
    döner — metadata gelmez.

14. **Sorgu doğrulaması istemci tarafındadır.** `EquityQuery('eq',
    ['nosuchfield', 1])` → `ValueError: Invalid field ...`, `EquityQuery('eq',
    ['region','xx'])` → `ValueError: Invalid EQ value "xx"`,
    `yf.screen(..., size=251)` → `ValueError: Yahoo limits query size to 250`.
    Üçü de **ağa çıkmadan** (doğrulama turunda `YfData.get/post` bombayla
    değiştirilerek sürüldü).

15. **`region='tr'` geçerlidir.** `EQUITY_SCREENER_EQ_MAP['region']` 59 bölge
    taşıyor. `EquityQuery('and', [eq(region,tr), gt(intradayprice,0)])` →
    **`total=628`**. `sortField='ticker', sortAsc=True` ile
    `A1CAP.IS, A1YEN.IS, AAGYO.IS, ACSEL.IS, ADEL.IS, ...`;
    **`sortAsc` verilmezse azalan** gelir (`ZRGYO.IS, ZOREN.IS, …`) — K15.

16. **Sembol kodları dar ve ASCII'dir.** 9.243 benzersiz sembolde: max uzunluk
    **17** (`^XAUSEK1630GMTSEK`), ASCII dışı **0**, harf/rakam dışı
    karakterler: `.` (5.114), `=` (616), `-` (150), **`^` (93)**, `+` (5),
    `&` (1), `_` (1). `SymbolType()` = `VARCHAR(32) ascii_bin` yeterli.
    **`^` endeks sembollerinde zorunludur** — herhangi bir doğrulama regex'i
    onu kapsamalıdır, yoksa endeksler toptan reddedilir.

17. **`screen` her satırda `symbol` döndürür.** 300 satırda istisnasız.

### 4.2 Çürütülen varsayımlar

1. **"`cache_get` yanıtı önbellekler."** Hayır. `data.py:512-513`:
   ```python
   def cache_get(self, url, params=None, timeout=30):
       return self.get(url, params, timeout)
   ```
   Düz bir takma ad.

2. **"`Lookup` nesnesi tek istekle sekiz tipi verir."** Hayır. `Lookup._cache`
   anahtarı `(lookup_type, count)`'tur; sekiz özellik **sekiz ayrı HTTP
   isteği** yapar. Maliyet 8×.

3. **"`offset` ve `count` birlikte çalışır."** Hayır, ve sessizce başarısız
   olur:

   | Çağrı | Dönen satır |
   |---|---|
   | `screen('top_mutual_funds', count=250)` | 250 |
   | `screen('top_mutual_funds', offset=250, count=250)` | **25** |
   | `screen('top_mutual_funds', offset=250, size=250)` | 250 |

   Nedeni `screener.py`'de: `offset` verilip sorgu `str` olduğunda kod
   predefined GET yolundan custom POST yoluna geçiyor (`defaults={}`), POST
   gövdesi `size` alanını kullanıyor ve `size=None` kalıyor — Yahoo
   varsayılan 25'e düşüyor. **Hata verilmez.**

4. **"`type=all&count=1000` her zaman yedi tipin birleşimini verir."**
   HAYIR — bu tasarımın ilk hâlindeki **çekirdek maliyet kararıydı** ve tek
   bir dar terimle (`BTC`) ölçülmüştü. §4.1/8: `GOLD` ve `TECH`'te `all`
   ~1.000'de kırpılıyor, tipli birleşim 3–4 kat geniş, fark `GOLD`'da **iki
   yönlü**. K6 adaptif hâle getirildi.

5. **"Araştırma raporu kimlikleri domain ve search'te ayrı uzaylardır."**
   Hayır. Biçim aynı: `<SAĞLAYICI>_<KAYNAK_KİMLİK>_<Tür>_<epoch_ms>`.
   **Orta segment sayı değildir**: `ARGUS_48138_…` sayısal ama
   `MS_0P000000GY_…` alfanümerik bir Morningstar kimliği. `report_id`
   üzerinde sayısal ayrıştırma varsayımı yapılmamalıdır.

6. **"Search haberi `Ticker.news` ile aynı gövdeyi taşır."** Hayır. Search:
   `uuid`, `title`, `publisher`, `link`, `providerPublishTime` (epoch
   **saniye**), `type`, `relatedTickers`, `thumbnail` — 8 anahtar.
   `Ticker.news`: `id` + `content` altında 17 anahtar (`summary`,
   `description`, `canonicalUrl`, `displayTime`, `pubDate` ISO metin, …).
   Kör upsert zengin satırı NULL'lardı.

7. **"Ekran `total`'ı gün içinde sabittir."** Hayır. `day_gainers` iki ölçüm
   arasında `122 → 117`. Kadro gün içinde değişir; `replace_scope` zorunlu.

8. **"`lists` bloğu tek şekillidir ve yalnız sayı taşır."** Hayır, iki şekil
   var ve ikisi de üyeliği çözecek kimlik taşıyor (§4.1/6).

9. **"Sekiz tipin kolon seti birebir özdeştir."** Hayır, üst küme olarak
   ortaktır (§4.1/9). Bu, K6'nın ikinci dayanağı olarak kullanılamaz.

10. **"`Search` varsayılan çağrısı araştırma raporlarını getirir."** Hayır,
    `include_research=False` varsayılandır (§4.1/5).

11. **"`screenerFieldResults` veri taşır."** Ölçülen her sorguda `[]`.

### 4.3 Sarmalayıcı asimetrileri

Aynı mantıksal alanın iki uçta farklı tip gelmesi. Ortak dönüştürücü
varsayılırsa **sessiz NULL** üretir.

| Alan | `Sector.research_reports` | `Search.research` |
|---|---|---|
| `reportDate` | ISO metin: `'2026-09-04T11:21:41Z'` | epoch ms: `1788306475000` |
| Başlık alanı | `headHtml` + `reportTitle` | `reportHeadline` |
| Yazar | **yok** | `author` |
| `reportType` | var | **yok** |
| Hedef fiyat / derece | var | **yok** |

Screener kotasyonunda da tip karışıklığı ölçüldü:

| Alan | Tip | Örnek |
|---|---|---|
| `dividendDate` | epoch saniye (int) | `1788306475` |
| `firstTradeDateMilliseconds` | epoch **ms** (int) | `871651800000` |
| `regularMarketTime` | epoch saniye (int) | `1788540654` |
| `ipoExpectedDate` | **ISO tarih metni** | `'2020-05-08'` |
| `nameChangeDate` | **ISO tarih metni** | `'2026-09-03'` |
| `corporateActions` | **liste** | `[{'header': 'Delisting', ...}]` |

`ipoExpectedDate` ve `nameChangeDate` "Date" adı taşıdığı halde epoch
**değildir**. `epoch_s` kind'ı verilseydi ikisi de sessizce NULL olurdu.

### 4.4 İstek maliyeti (ölçüldü)

19 predefined ekranın `total` değerleri, 2026-09-04:

| Ekran | `total` | sayfa (250) | sayfa (cap=4) |
|---|---:|---:|---:|
| `most_shorted_stocks` | 4.022 | 17 | 4 |
| `top_mutual_funds` | 1.788 | 8 | 4 |
| `top_etfs_us` | 523 | 3 | 3 |
| `top_performing_etfs` | 523 | 3 | 3 |
| `aggressive_small_caps` | 484 | 2 | 2 |
| `bond_etfs` | 382 | 2 | 2 |
| `portfolio_anchors` | 260 | 2 | 2 |
| `undervalued_growth_stocks` | 249 | 1 | 1 |
| `technology_etfs` | 182 | 1 | 1 |
| `conservative_foreign_funds` | 149 | 1 | 1 |
| `solid_large_growth_funds` | 143 | 1 | 1 |
| `day_losers` | 128 | 1 | 1 |
| `most_actives` | 123 | 1 | 1 |
| `day_gainers` | 117 | 1 | 1 |
| `undervalued_large_caps` | 97 | 1 | 1 |
| `solid_midcap_growth_funds` | 92 | 1 | 1 |
| `growth_technology_stocks` | 54 | 1 | 1 |
| `high_yield_bond` | 54 | 1 | 1 |
| `small_cap_gainers` | 50 | 1 | 1 |
| **TOPLAM** | | **49** | **32** |

Custom: `region=tr` equity → `total=628` → 3 sayfa. Sınırsız ETF taraması
(`region=us`, `intradayprice>10`) → `total=5.712` → **23 sayfa**;
`yf_screen_max_pages` bunun kaza eseri koşmasını engeller.

Sembol tarafı: `search` 1 istek/terim. `lookup` **1 ya da 8** istek/terim
(K6 adaptif): sembol terimlerinde neredeyse daima 1, geniş serbest
terimlerde 8.

### 4.5 Screener kotasyon alan haritası

150 satırda **100 farklı alan**; **75'i `INFO_FIELDS` ile aynı kaynak
anahtarını** taşıyor ve `ticker_info` ile **aynı kolon adını** ve **aynı
kind'ı** devralır. Kalan 25: 23 yeni tipli alan + `symbol` (PK) +
`corporateActions` (liste → `raw_json`).

Kolon adı üretmek yerine devralmak, `ticker_info` ile `screen_quotes`
arasında JOIN'siz karşılaştırmayı mümkün kılar ve iki tablonun ayrışmasını
engeller.

**Doluluk uyarısı:** "her satırda dolu" alan sayısı **örnekleme bağımlıdır**
— ilk turda 51, bağımsız doğrulama turunda 49 ölçüldü (fark:
`averageDailyVolume10Day` / `averageDailyVolume3Month` bazı fonlarda eksik).
Sağlam olan bölünme **100 / 75 / 25**'tir; testler bunun üzerine kurulur,
doluluk oranı üzerine değil (§10.2).

**Yeni tipli alanlar (23):**

| Kaynak anahtarı | Kolon | Kind | ~Doluluk | Not |
|---|---|---|---:|---|
| `fulldayPrice` | `fullday_price` | `dec` | 100% | |
| `fulldayChange` | `fullday_change` | `dec` | 100% | |
| `fulldayChangePercent` | `fullday_change_percent` | `dec` | 100% | |
| `fiftyDayAverageChange` | `fifty_day_average_change` | `dec` | 100% | |
| `fiftyDayAverageChangePercent` | `fifty_day_average_change_percent` | `dec` | 100% | |
| `fiftyTwoWeekHighChange` | `fifty_two_week_high_change` | `dec` | 100% | |
| `fiftyTwoWeekHighChangePercent` | `fifty_two_week_high_change_percent` | `dec` | 100% | |
| `fiftyTwoWeekLowChange` | `fifty_two_week_low_change` | `dec` | 100% | |
| `fiftyTwoWeekLowChangePercent` | `fifty_two_week_low_change_percent` | `dec` | 100% | |
| `twoHundredDayAverageChange` | `two_hundred_day_average_change` | `dec` | 100% | |
| `twoHundredDayAverageChangePercent` | `two_hundred_day_average_change_percent` | `dec` | 100% | |
| `customPriceAlertConfidence` | `custom_price_alert_confidence` | `str16` | 100% | Ölçülen: `HIGH`, `LOW` |
| `trailingThreeMonthReturns` | `trailing_three_month_returns` | `dec` | 64% | |
| `annualReturnNavY3` | `annual_return_nav_y3` | `dec` | 62% | |
| `annualReturnNavY5` | `annual_return_nav_y5` | `dec` | 62% | |
| `peTTM` | `pe_ttm` | `dec` | 50% | |
| `yieldTTM` | `yield_ttm` | `dec` | 50% | |
| `lastClosePriceToNNWCPerShare` | `last_close_price_to_nnwc_per_share` | `dec` | 32% | |
| `lastCloseTevEbitLtm` | `last_close_tev_ebit_ltm` | `dec` | 32% | |
| `trailingThreeMonthNavReturns` | `trailing_three_month_nav_returns` | `dec` | 31% | |
| `ipoExpectedDate` | `ipo_expected_date` | **`dt`** | 6% | **ISO metin**, epoch değil (§4.3) |
| `nameChangeDate` | `name_change_date` | **`dt`** | 4% | **ISO metin**, epoch değil |
| `prevName` | `prev_name` | `str255` | 4% | Baştaki boşluk ölçüldü: `' LiveWire Group, Inc.'` → `nz.to_str` kırpar |

Satır maliyeti `KINDS.row_cost` toplamıyla ~6 KB; InnoDB'nin 65.535 baytlık
satır sınırının çok altında.

---

## 5. Şema

**On yeni tablo, bir yeniden adlandırma, iki tabloya toplam dört kolon.**

### 5.1 `discovery_asof_state` — keşif tarafının kapı tablosu

PK `(query_term, dataset)`.

| Kolon | Tip | Not |
|---|---|---|
| `query_term` | `AsciiKeyType(64)` | **FK YOK** (K3a) |
| `dataset` | `AsciiKeyType(32)` | |
| `as_of_date` | `Date` | Son değişimin as-of günü |
| `content_hash` | `HashType()` | |
| `row_count` | `Integer` | |
| `first_seen_at` | `TsType()` | Yalnız INSERT'te yazılır |
| `fetched_at` | `TsType()` | Son doğrulama zamanı |

Index: `ix_discovery_asof_dataset_date (dataset, as_of_date)`.

**`asof_state` neden kullanılamaz (K3a):** o tablonun `symbol` kolonu
`symbol_fk_column` ile tanımlıdır (`models/asof.py:33`), yani
`symbols.symbol`'a `ON DELETE RESTRICT` FK taşır. `yfin discover term
"Turkish Airlines"` kapı satırını yazamaz — `symbols`'ta böyle bir satır
yoktur ve `ERROR 1452` alır. Bu, SI'nin `domain_asof_state` için karşılaştığı
duvarın aynısıdır ve aynı biçimde çözülür: `AsOfGate`'in
`asof_gate_table` / `asof_gate_key_columns` / `gate_identity` uzatma
noktaları (`domain/base.py:146-163`). **Yeni bir kapı SINIFI yazılmaz** — K3
korunur.

`query_term` `AsciiKeyType(64)`: sembol terimleri en fazla 17 karakter
(§4.1/16), serbest terimler CLI'da **64 ASCII karakterle** sınırlanır ve aşan
reddedilir. Sessiz kırpma iki farklı terimi aynı kapı satırına düşürüp
birinin verisini diğerinin sanmasına yol açardı.

### 5.2 `search_quotes`

As-of. PK `(query_term, as_of_date, symbol)`.

| Kolon | Tip | Not |
|---|---|---|
| `query_term` | `AsciiKeyType(64)` | Sembol döngüsünde sembol kodu, serbest terimde terim |
| `as_of_date` | `Date` | Koşunun UTC günü |
| `symbol` | `SymbolType()` | FK **yok** (K9) |
| `rank` | `SMALLINT` | Yanıttaki **0-tabanlı** sıra (§5.14) |
| `score` | `PriceType()` | Ölçülen aralık 12,2 – 16.067.500,0 |
| `quote_type` / `type_disp` | `str32` / `str64` | |
| `exchange` / `exch_disp` | `str32` / `str64` | |
| `short_name` / `long_name` | `str128` / `str255` | |
| `sector` / `sector_disp` | `str64` | Yalnız EQUITY (§4.1/2) |
| `industry` / `industry_disp` | `str128` | Yalnız EQUITY |
| `disp_sec_ind_flag` / `is_yahoo_finance` | `bool` | |
| `prev_name` / `name_change_date` | `str255` / `dt` | |
| `is_known` | `bool` | §8.3 |
| `fetched_at` | `TsType()` | |
| `raw_json` | `RawJsonType()` | Haritalanmayan her anahtar |

Index: `ix_search_quotes_symbol (symbol)` — FK olmadığı için **açıkça**
tanımlanır (S §5.6).

**Sembolsüz satırlar bu tabloya girmez** (K14): normalize adımı
`if "symbol" not in quote: continue` süzgeci uygular. `.response` ham gövdesi
kullanıldığı için `yfinance`'in kendi süzgeci devrede değildir.

### 5.3 `search_lists`

As-of. PK `(query_term, as_of_date, list_key)`.

`list_key` `AsciiKeyType(128)`: `ALGO_WATCHLIST` şeklinde `slug`,
`PREDEFINED_SCREENER` şeklinde `canonicalName`. Ölçülen en uzun:
`most-bought-by-activist-hedge-funds` = 35 karakter.

| Kolon | Kind | Hangi şekilde |
|---|---|---|
| `rank` | `SMALLINT` | ikisi |
| `list_type` | `str32` | ikisi — **ayırıcı**: `ALGO_WATCHLIST` / `PREDEFINED_SCREENER` |
| `name` | `str255` | `ALGO_WATCHLIST`: `name`; `PREDEFINED_SCREENER`: `title` |
| `score` | `dec` | ikisi |
| `icon_url` | `text` | ikisi |
| `brand_slug` | `str64` | yalnız `ALGO_WATCHLIST` |
| `pf_id` | `str128` | yalnız `ALGO_WATCHLIST` |
| `user_id` | `str64` | yalnız `ALGO_WATCHLIST` |
| `symbol_count` | `int` | yalnız `ALGO_WATCHLIST` |
| `daily_percent_gain` | `dec` | yalnız `ALGO_WATCHLIST` |
| `follower_count` | `int` | yalnız `ALGO_WATCHLIST` |
| `yahoo_id` | `str64` | yalnız `PREDEFINED_SCREENER` (`id`) |
| `total` | `int` | yalnız `PREDEFINED_SCREENER` |
| `is_premium` | `bool` | yalnız `PREDEFINED_SCREENER` |
| `fetched_at`, `raw_json` | | ikisi |

Tek tablo + ayırıcı ENUM, kod tabanının kendi kuralıdır
(`institutional_holders` + `mutualfund_holders` deseni). İki şeklin ortak
alanları dört tanedir; ayrı tablolar bunları çoğaltırdı.

**Üyelik tablosu yoktur** (§1). Listenin sembolle ilişkisi `query_term`
üzerindendir.

### 5.4 `research_reports` — yeniden adlandırılmış paylaşılan varlık

`domain_research_reports` tablosu `research_reports` adını alır (K8). Mevcut
kolonlar korunur; iki kolon eklenir:

| Yeni kolon | Tip | Dolduran yol |
|---|---|---|
| `author` | `String(128)` | Yalnız Search; domain yolunda NULL |
| `report_headline` | `String(512)` | Yalnız Search (`reportHeadline`); domain yolunda **NULL kalır** — domain başlığı mevcut `head_html` / `report_title` kolonlarında durur |

`report_ts_utc` iki farklı dönüştürücüyle yazılır: domain yolunda ISO metin,
Search yolunda epoch ms (§4.3).

`report_id` `AsciiKeyType(64)` olarak kalır. Biçim
`<SAĞLAYICI>_<KAYNAK_KİMLİK>_<Tür>_<epoch_ms>`; **orta segment alfanümerik
olabilir** (`MS_0P000000GY_…`) — üzerinde sayısal ayrıştırma yapılmaz.

**Yeniden adlandırmanın etki alanı** (SI katmanı canlı olduğu için §12.1 ile
aynı değişiklikte gider):

| Dosya | Değişiklik |
|---|---|
| `src/yfin/datasets/domain/common.py:34-35` | `REPORTS_TABLE = "research_reports"` (sabit; `REPORT_LINKS_TABLE` değişmez) |
| `src/yfin/prune.py:52, 208, 238-240` | Yetim-rapor budama fonksiyonunun tablo adı ve yorumları |
| `src/yfin/datasets/asof_base.py:63` | Yorum satırındaki tablo adı |
| `src/yfin/datasets/domain/profile.py:4-5` | Modül docstring'i |
| `src/yfin/models/domains.py` | `__tablename__` + sınıf adı `DomainResearchReport` → `ResearchReport` |

`domain_report_links` tablosu **adını korur** (domain'e özgü bir bağdır);
yalnızca FK hedef tablo adı güncellenir.

### 5.5 `search_report_hits`

PK `(query_term, as_of_date, report_id)`. `rank`, `fetched_at`.
`report_id` → `research_reports.report_id` FK (`ON UPDATE CASCADE`,
`ON DELETE CASCADE`) — `domain_report_links`'in kardeşi. Burada FK
**vardır**: `report_id` sembol değildir ve ebeveyn satırı aynı transaction'da,
`gated` yazımların **öncesinde** (§6.2.1 sırası) yazılır.

### 5.6 `lookup_results`

As-of. PK `(query_term, as_of_date, symbol)`.

`source_rank` (`int`; kaynağın `rank` alanı — bir **sıra değil**, Yahoo'nun
kendi sıralama skoru, ölçülen örnek 30007), `rank` (`SMALLINT`; yanıttaki
0-tabanlı sıra), `lookup_type` (`str24`; hangi çağrıdan geldiği — K6 adaptif
olduğu için gereklidir), `quote_type` (`str32`), `exchange` (`str32`),
`short_name` (`str128`), `industry_name` (`str128`), `industry_link` (`text`),
`fullday_price` / `fullday_change` / `fullday_change_percent` (`dec`),
`regular_market_price` / `regular_market_change` /
`regular_market_percent_change` (`dec`), `is_known` (`bool`), `fetched_at`,
`raw_json`.

**`lookup_type` kolonu VARDIR.** Tasarımın ilk hâlinde yoktu — gerekçesi
"tek `all` çağrısı yapılır, tip zaten `quote_type`'ta" idi. K6 adaptif
olduğu için artık satırın hangi çağrıdan geldiği bilinmelidir: aynı sembol
`equity` ve `etf` çağrılarının ikisinde birden dönebiliyor (§4.1/8'de
`BTC` için üç sembol iki tipte listelendi) ve PK'da olmadığı için son yazan
kazanır — hangi çağrının yazdığı denetlenebilir olmalıdır.

`industry_name` / `industry_link` **yalnız `equity` belgelerinde** dolu
(§4.1/9); nullable.

Index: `ix_lookup_results_symbol (symbol)`.

### 5.7 `lookup_totals`

As-of. PK `(query_term, as_of_date, lookup_type)`. `total` (`int`),
`fetched_at`.

`lookup_type` `String(24)`; **dokuz** değer, `privateCompany` dahil
(§4.1/10). Kaynak `lookupTotals` bloğudur, `LOOKUP_TYPES` sabiti **değil**.

Bu tablo eksiksizlik iddiasının kanıtıdır: `lookupTotals.all` 7.261
bildirirken `documents` 996 döndüğünde fark **kayıt altındadır** (§4.1/8) ve
K6'nın adaptif dalını da tetikleyen sinyal budur.

### 5.8 `screens` — statik kimlik

PK `screen_key` `AsciiKeyType(32)`.

| Kolon | Tip | Not |
|---|---|---|
| `kind` | ENUM(`predefined`, `custom`) | |
| `quote_type` | ENUM(`EQUITY`, `MUTUALFUND`, `ETF`) | |
| `title` | `String(255)` | Custom'da `ScreenDef`'ten; predefined'da ilk GET sayfasından **tazelenir** (§4.1/13) |
| `description` | `Text` | Aynı |
| `sort_field` | `String(64)` | `ScreenDef`'ten (K15) |
| `sort_asc` | `bool` | `ScreenDef`'ten (K15) |
| `definition_json` | `RawJsonType()` | Custom'da `query.to_dict()`, predefined'da `rawCriteria` |
| `is_enabled` | `bool` | Koşu anındaki etkinliğin tek kaynağı |
| `created_at` / `updated_at` | `TsType()` | |

`domains` tablosunun kardeşi; `screens.py`'deki `ScreenDef` kümesinden
bootstrap edilir (§12.1).

**32 karakter sınırı bağlayıcıdır.** Ölçülen en uzun predefined ad
`conservative_foreign_funds` = 26. Sınır `sync_run_items.symbol`
kolonundan gelir (§5.13).

### 5.9 `screen_runs` — hash kapısı **ve** veri tablosu

PK `(screen_key, as_of_date)`.

| Kolon | Tip | Not |
|---|---|---|
| `total` | `int` | Yahoo'nun bildirdiği gerçek eşleşme sayısı |
| `fetched_rows` | `int` | Yanıttan alınan kotasyon sayısı |
| `row_count` | `int` | Kapı sözleşmesi kolonu: `screen_members` satır sayısı (`AsOfGate`/`HashGated` başlık deseni) |
| `page_count` | `SMALLINT` | Yapılan istek sayısı |
| `yahoo_id` | `String(64)` | GET yanıtındaki `id` |
| `version_id` | `int` | |
| `last_updated` | `dt` | GET yanıtındaki `lastUpdated` (epoch ms) |
| `criteria_json` | `RawJsonType()` | `rawCriteria` / `criteriaMeta` |
| `content_hash` | `HashType()` | **Yalnız kadro + sıra** (K4) |
| `fetched_at` | `TsType()` | |

`total` ≠ `fetched_rows` olduğunda ekran sayfa sınırına takılmıştır. Bu fark
**sessiz değil, kayıtlıdır**; `yfin screen list` onu gösterir.

### 5.10 `screen_members` — kapının çocuğu

PK `(screen_key, as_of_date, symbol)`. `rank` (`int`), `is_known` (`bool`),
`fetched_at`.

`rank` = `offset + sayfa içi 0-tabanlı indeks`, yani ekranın `sort_field`'ına
göre mutlak sıra; **ilk sembolün `rank`'i 0'dır** (§5.14).
`replace_scope` kapsamı `(screen_key, as_of_date)` — kapının
`gate_key_columns`'ından türetilir (`hash_gated.py:72,91`).

Index: `ix_screen_members_symbol (symbol)`.

### 5.11 `screen_quotes` — ekrandan bağımsız

PK `(symbol, as_of_date)`. `SCREENER_QUOTE_FIELDS`'ın ürettiği ~98 kolon +
`is_known`, `fetched_at`, `raw_json`.

Kapının silme kapsamına **girmez** (K5): `HashGatedDataset.upsert`'te
`other_writes` yolundan düz upsert edilir. Kadro değişmediğinde bile yazılır
— fiyatlar değişmiştir.

Yan ürün olarak keşfedilen **her** sembol için günlük kotasyon enstantanesi
verir; `ticker_info` ile aynı kolon adlarını taşıdığı için karşılaştırma
JOIN'siz yapılabilir (§4.5).

`screen_key` kolonu **yoktur** — budama bunu hesaba katmalıdır (§12.2).

### 5.12 `symbols` genişletmesi

İki kolon: `discovered_by` (`String(16)`, `server_default='manual'`) ve
`discovered_at` (`TsType()`). Değerler: `manual`, `search`, `lookup`,
`screener`.

**Update kapsamı dışı kolonlar** (K10): `is_active`, `unknown_streak`,
`discovered_by`, `discovered_at`. Keşif yazımı bunlara yalnızca INSERT'te
değer verir; `ON DUPLICATE KEY UPDATE` onlara **dokunmaz**.

**Güncellenen kolonlar yola göre değişir** — §8.5'in `news` için kurduğu
"yalnız doldurduğun kolonu güncelle" kuralının kardeşi. Her yol yalnız
gerçekten döndürdüğü alanları `update_columns`'a koyar:

| Yol | `update_columns` |
|---|---|
| `search` | `short_name`, `long_name`, `exchange`, `quote_type`, `last_seen_at` |
| `lookup` | `short_name`, `exchange`, `quote_type`, `last_seen_at` |
| `screener` | `short_name`, `long_name`, `exchange`, `full_exchange_name`, `quote_type`, `currency`, `timezone`, `first_trade_date`, `last_seen_at` |

Ortak listede olsaydı `lookup` her koşuda `long_name` ve `currency`'yi
NULL'lardı — `search`/`screener`'ın yazdığını silerek.

### 5.13 `sync_run_items` — değişiklik yok

Screener tarafında kapsam etiketi `screen_key` olarak mevcut `symbol`
kolonuna yazılır (`VARCHAR(32)`'ye sığar, §5.8). `region` kolonu **NULL
kalır** — ekran bir bölge değildir.

Sembol tarafında `search`/`lookup` sembol kırılımı üretir; serbest terim
koşusunda `symbol` alanına terimin kendisi yazılır (64 karakterlik terim
`VARCHAR(32)`'ye sığmayabilir → §8.2'de kırpılarak yazılır, çünkü burası
**denetim** kaydıdır, anahtar değil; gerçek anahtar
`discovery_asof_state.query_term`'dedir).

### 5.14 `rank` semantiği — tek tanım

Beş tabloda `rank` geçiyor; anlamı **her yerde aynıdır**: yanıttaki
**0-tabanlı** sıra.

| Tablo | `rank` |
|---|---|
| `search_quotes` | `quotes` bloğundaki sıra (sembolsüz satırlar elendikten **sonra**) |
| `search_lists` | `lists` bloğundaki sıra |
| `search_report_hits` | `researchReports` bloğundaki sıra |
| `lookup_results` | `documents` içindeki sıra |
| `screen_members` | `offset + sayfa içi indeks` (mutlak) |

Kaynağın kendi `rank` alanı (Lookup'ta, örn. 30007) bir sıra **değildir**;
karışmasın diye `lookup_results.source_rank` adını alır (§5.6).

`screen_members.rank` hash gövdesindedir (§6.3): kadro aynı kalıp sıralama
değiştiğinde bu **gerçek bir değişimdir**.

### 5.15 Tablo özeti

| Tablo | Anahtar | Yazma modu | Kapı |
|---|---|---|---|
| `discovery_asof_state` | (query_term, dataset) | upsert | **kapının kendisi** |
| `search_quotes` | (query_term, as_of_date, symbol) | upsert | `discovery_asof_state` |
| `search_lists` | (query_term, as_of_date, list_key) | upsert | `discovery_asof_state` |
| `search_report_hits` | (query_term, as_of_date, report_id) | upsert | `discovery_asof_state` |
| `research_reports` | (report_id) | upsert | — (kapı dışı, §6.2.1) |
| `lookup_results` | (query_term, as_of_date, symbol) | upsert | `discovery_asof_state` |
| `lookup_totals` | (query_term, as_of_date, lookup_type) | upsert | `discovery_asof_state` |
| `screens` | (screen_key) | upsert | — (statik; metadata ilk GET sayfasından tazelenir) |
| `screen_runs` | (screen_key, as_of_date) | upsert | **kapının kendisi** |
| `screen_members` | (screen_key, as_of_date, symbol) | replace_scope | `screen_runs` |
| `screen_quotes` | (symbol, as_of_date) | upsert | — (kapı dışı, K5) |
| `symbols` | (symbol) | upsert, yola özgü dar kapsam | — (kapı dışı, §6.2.1) |
| `news` / `news_symbols` | mevcut | upsert, dar kapsam | — (kapı dışı, §6.2.1) |

---

## 6. Sözleşme değişiklikleri

### 6.1 `scope="variant"`

```python
# datasets/market/base.py
MarketScope = Literal["global", "region", "variant"]


@dataclass
class MarketContext:
    fetched_at: datetime
    start: date
    end: date
    region: str | None = None
    variant: str | None = None          # YENI
    _cache: dict[str, Any] = field(default_factory=dict, repr=False)

    def for_region(self, region: str) -> MarketContext:
        return self._clone(region=region)

    def for_variant(self, variant: str) -> MarketContext:
        """Ayni onbellegi paylasan, varyanti ayarlanmis baglam.

        `region` alanina YAZILMAZ: ekran bir bolge degildir.
        """
        return self._clone(variant=variant)

    def _clone(self, **changes: Any) -> MarketContext:
        """TEK klonlama noktasi (DomainContext._clone deseni).

        Mevcut `for_region` alanlari ELLE kopyaliyor; yeni bir alan
        eklendiginde orada da eklenmezse SESSIZCE duser. `_clone` bu
        siniflandirmayi tek yere indirir ve `variant` eklemesi
        `for_region`'i de otomatik olarak dogru tutar.
        """
```

**`for_region`'ın elle alan kopyalaması `_clone`'a çekilir.** Bugünkü gövde
`MarketContext(fetched_at=..., start=..., end=..., region=region)` kuruyor ve
`_cache`'i sonradan bağlıyor; `variant` eklenip orada kopyalanmazsa bölge
dalında sessizce düşerdi. `DomainContext._clone` (`domain/base.py:59`) aynı
sorunu aynı biçimde çözmüş; deseni ödünç alıyoruz.

```python
class GlobalDataset[RawT](ABC):
    name: str
    depends_on: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    scope: MarketScope = "global"

    def variants(self, settings: Settings, session: Session) -> Sequence[str]:
        """scope == "variant" ise dis dongunun anahtarlari.

        `session` ZORUNLUDUR: varyant kumesi `screens` tablosundan
        (is_enabled) okunur. `Settings` bir Pydantic ayar nesnesidir ve DB
        erisimi tasimaz -- yalniz `settings` alan bir imza `screens`
        tablosunu OKUYAMAZDI.
        """
        return ()
```

`market_runner.run_market_sync` üçüncü dalı ekler. `_run_turn`'ün dördüncü
parametresi bugün de bir **kapsam etiketidir** (`scope_label`); bölge dalında
bölge adı, varyant dalında ekran anahtarı geçer ve `sync_run_items.symbol`
kolonuna yazılır (§5.13):

```python
        if dataset.scope == "region":
            for region in regions:
                items.extend(_run_turn(factory, dataset, base_ctx.for_region(region), region, tracker))
        elif dataset.scope == "variant":
            with factory() as session:
                keys = dataset.variants(cfg, session)
            for key in keys:
                items.extend(_run_turn(factory, dataset, base_ctx.for_variant(key), key, tracker))
        else:
            items.extend(_run_turn(factory, dataset, base_ctx, GLOBAL_SCOPE_MARKER, tracker))
```

`variants()` **kendi kısa ömürlü session'ında** okunur ve liste
maddileştirilir; tur transaction'ları (`_run_turn`) o session'ı tutmaz.

Bölge dalı davranışı **hiç değişmez**; `tests/unit/test_market_scope_variant.py`
bunu regresyon olarak sürer.

Döngünün dataset'in **dışında** olmasının gerekçesi `market/base.py`
başlığındakiyle aynıdır: `sync_run_items` granülerliği doğal olarak
(dataset × ekran × tablo) olur. İçeri gömülseydi 19 ekran tek bir denetim
satırına düşer, bir ekranın patlaması diğerlerini de `failed` gösterirdi.

### 6.2 Hash kapıları — yeni sınıf yok, yeni tablo var

**`search` ve `lookup` → `AsOfDataset`** (`AsOfGate` mixin'i), ama kendi kapı
tablosuyla — SI'nin `DomainAsOfDataset` için kullandığı uzatma noktalarının
aynısı (`domain/base.py:146-163`):

```python
DISCOVERY_GATE_TABLE = "discovery_asof_state"
DISCOVERY_GATE_KEY_COLUMNS = ("query_term", "dataset")


class DiscoveryDataset[RawT](AsOfDataset[RawT]):
    asof_gate_table = DISCOVERY_GATE_TABLE
    asof_gate_key_columns = DISCOVERY_GATE_KEY_COLUMNS

    def gate_identity(self, result: NormalizedResult) -> dict[str, Any]:
        return {"query_term": _first_row(result)["query_term"], "dataset": self.name}
```

`produces` bildirimi `asof_produces(..., gate=DISCOVERY_GATE_TABLE)` ile
verilir — yardımcının `gate` parametresi tam bu iş için vardır
(`asof_base.py:73-84`).

**`screener` → `HashGatedDataset`**: `gate_table="screen_runs"`,
`child_table="screen_members"`, `gate_key_columns=("screen_key",
"as_of_date")`. `screen_quotes`, `screens` ve `symbols` yazımları
`other_writes` yolundan geçer, yani kapıdan **bağımsız** upsert edilir.
`HashGatedDataset` tek bir `child_table` destekliyor (`hash_gated.py:29`) ve
bu tasarım o kısıtı genişletmeye ihtiyaç duymaz.

#### 6.2.1 Kapı kapsamı — dört tablo kapının DIŞINDADIR

`AsOfGate._gate_write` kapı satırının `as_of_date` ve `fetched_at` alanlarını
`_first_row(result)`'tan, yani `writes` listesindeki **ilk dolu** veri
satırından okur (`asof_base.py:151-158`); `gate_identity` override'ı da aynı
satırdan `query_term` bekler.

**Dört tablo bu sözleşmeyi karşılamaz:**

| Tablo | Eksik kolon |
|---|---|
| `symbols` | `query_term`, `as_of_date`, `fetched_at` — üçü de yok |
| `news` | üçü de yok |
| `news_symbols` | üçü de yok |
| `research_reports` | `query_term` yok (`as_of_date`/`fetched_at` var) |

"Sıralamaya dikkat edilir" demek yeterli **değildir**: `search_quotes` boş
kalabilirken `news` ya da `research_reports` dolu olabilir
(`"Turkish Airlines"` ölçümü, §4.1/5) — o durumda ilk dolu write onlardan
biri olur ve sıralama disiplini işe yaramaz.

Bu yüzden `DiscoveryDataset` `upsert`'i açıkça ikiye böler:

```python
    # Kapinin DISINDA kalan tablolar. Dordu de PAYLASILAN ya da evren
    # kaydidir: bir arama teriminin icerik hash'i onlarin yazilip
    # yazilmayacagini belirleyemez.
    UNGATED_TABLES = frozenset({"symbols", "news", "news_symbols", "research_reports"})

    def upsert(self, writer: RowWriter, result: NormalizedResult) -> WriteStats:
        ungated = [w for w in result.writes if w.table in self.UNGATED_TABLES]
        gated = [w for w in result.writes if w.table not in self.UNGATED_TABLES]

        # 1. Kapisiz yazimlar ONCE: `search_report_hits`in FK'si
        #    `research_reports`e bakiyor, ebeveyn once yazilmalidir (S5.5).
        stats = WriteStats(skipped=dict(result.skipped))
        for write in ungated:
            apply_write(writer, write, stats)

        # 2. Kapiya YALNIZCA query_term + as_of_date + fetched_at tasiyan
        #    tablolar girer. `skipped` BOS gecirilir: dis `stats` onu zaten
        #    tohumladi, iki kez sayilirsa `rows_skipped` denetimi bozulur.
        gated_result = NormalizedResult(writes=gated, skipped={})
        gate_stats = super().upsert(writer, gated_result)
        return merge_stats(stats, gate_stats)
```

`merge_stats` **yeni bir yardımcıdır** ve `datasets/base.py`'ye eklenir
(§14/1): `attempted`/`verified`/`skipped` sözlüklerini anahtar bazında
toplar. Kod tabanında bugün karşılığı yoktur.

**`gated_result`'a `skipped={}` geçirilmesi zorunludur.** İki nedenle:
(a) dış `stats` `result.skipped` ile zaten tohumlanmıştır, ikisi toplanırsa
sayaç ikiye katlanır; (b) `NormalizedResult.is_empty` "satır yok **ve**
skipped boş" demektir (`base.py:92-94`) — `gated` satırsız ama `skipped`
doluyken `is_empty` False olur ve `_first_row` `ValueError` fırlatarak hücreyi
`failed`'e düşürürdü.

Bu ayrımın ikinci faydası: hash gövdesi artık **yalnızca arama sonucunu**
kapsar. `news` gövdeye girseydi yeni bir haber her gün hash'i değiştirir ve
`search_quotes` kadrosu hiç değişmediği halde her gün yeniden yazılırdı.

`gated` listesinin tamamı boşsa kapı satırı yazılmaz (`AsOfGate.upsert`
`is_empty` dalı) ama `ungated` yazımlar yapılmıştır — durum tablo bazında
türetilir (§9.2). `test_search_gate_scope.py` bunu sürer.

### 6.3 Hash gövdesi — K4'ün uygulaması

`screen_runs.content_hash`, `screen_members` satırlarının
`(screen_key, as_of_date, symbol, rank)` gövdesinden hesaplanır.
`screen_quotes` **gövdeye girmez**.

Girseydi: `regularMarketPrice` her koşuda oynadığı için hash hiçbir zaman
eşitlenmez, `skipped` durumu hiç üretilmez ve mekanizma sessizce ölürdü.
`VOLATILE_COLUMNS`'un (`asof_base.py:65`) çözdüğü sorunun aynısı;
`test_screen_hash_body.py` regresyon kilidi.

### 6.4 Kayıt — ayara bağlı, **ve import zinciri**

```python
# datasets/discovery/search.py sonu
if get_settings().yf_discovery_enabled:
    register(SearchDataset())
```

Modül düzeyindeki `register(...)` yalnızca modül **import edilirse** koşar.
`datasets/__init__.py` docstring'i bunu açıkça söylüyor: *"Alt modulleri
import ederek registry kayitlarini tetikler"*. İki import satırı
**zorunludur**, yoksa dataset'ler hiçbir zaman kaydolmaz ve `yfin screen sync`
sessizce sıfır dataset koşar:

```python
# src/yfin/datasets/__init__.py — import listesine:
from yfin.datasets import (..., discovery, ...)

# src/yfin/datasets/market/__init__.py — import satırına:
from yfin.datasets.market import calendars, screener, status  # noqa: F401
```

`screener` piyasa registry'sine **koşulsuz** kaydedilir: `yfin screen sync`
ayrı bir komuttur ve `screens` tablosu boşsa dataset hiç istek yapmaz.

### 6.5 Ekran tanımları — `screens.py`

Tanımın tek kaynağı `screens.py`; **koşu anındaki etkinliğin** tek kaynağı
`screens.is_enabled` kolonudur (seed'den sonra operatör onu DB'de
değiştirebilir).

```python
@dataclass(frozen=True, slots=True)
class ScreenDef:
    key: str                      # <= 32 ASCII karakter
    kind: Literal["predefined", "custom"]
    quote_type: Literal["EQUITY", "MUTUALFUND", "ETF"]
    title: str
    sort_field: str               # K15: acikca verilir
    sort_asc: bool = False        # K15
    description: str = ""
    is_enabled: bool = True       # seed degeri
    query: QueryBase | None = None   # predefined'da None (ad yeterli)
```

19 predefined `PREDEFINED_SCREENER_QUERIES`'ten **türetilir**, elle
yazılmaz; `test_screens_match_library.py` kümenin kütüphaneyle aynı olduğunu
sürer — kütüphane bir ekran eklediğinde ya da kaldırdığında sessizce
sapmayız.

Custom ekranlar aynı dosyada `EquityQuery` / `FundQuery` / `ETFQuery` ile
tanımlanır. §4.1/14 uyarınca doğrulama **istemci tarafındadır**: geçersiz bir
alan ya da bölge modül yüklenirken `ValueError` fırlatır, koşu ortasında
değil.

---

## 7. Dataset'ler

### 7.1 `search` — sembol ekseni, as-of

```
fetch(ctx):  yf.Search(ctx.symbol,
                       max_results=cfg.yf_search_max_results,
                       news_count=cfg.yf_search_news_count,
                       lists_count=cfg.yf_search_lists_count,
                       include_research=True,      # VARSAYILAN DEGIL (S4.1/5)
                       include_nav_links=False)
             .response                              # 1 istek

normalize -> symbols            (yeni semboller, pasif)     [kapi disi]
             news + news_symbols                            [kapi disi]
             research_reports                                [kapi disi]
             search_quotes                                   [kapili]
             search_lists                                    [kapili]
             search_report_hits                              [kapili]
```

```python
produces = asof_produces(
    "symbols", "news", "news_symbols", "research_reports",
    "search_quotes", "search_lists", "search_report_hits",
    gate=DISCOVERY_GATE_TABLE,
)
```

**`include_research=True` açıkça verilir** — varsayılanı `False`'tur
(§4.1/5) ve verilmezse `research_reports` / `search_report_hits` **hiç satır
almaz**, üstelik §9.6 eksiksizlik kanıtı bunu yakalamaz.
`include_nav_links=False`: `nav` kapsam dışı (§1).

**Sembolsüz satırlar elenir** (K14): `.response` ham gövdesi kullanıldığı
için `yfinance`'in `.quotes` süzgeci devrede değildir;
`if "symbol" not in quote: continue` açıkça uygulanır.
`test_search_crunchbase_filter.py` bunu sürer.

**Boş sonuç ayrımı.** `quotes` boş ama `news` dolu olabilir. Durum tablo
bazında türetilir (§9.2): `search_quotes` `empty`, `news` `ok`.

### 7.2 `lookup` — sembol ekseni, as-of, **adaptif çağrı**

```
fetch(ctx):
    raw = yf.Lookup(term)._fetch_lookup("all", cfg.yf_lookup_count)   # 1. istek
    totals = raw["finance"]["result"][0]["lookupTotals"]

    if totals["all"] > cfg.yf_lookup_all_threshold:
        # `all` KIRPILDI (S4.1/8). Yedi tipli cagriya dusulur; her belge
        # hangi cagridan geldigiyle birlikte tutulur.
        for t in LOOKUP_TYPES_WITHOUT_ALL:            # 7 istek daha
            raw_t = yf.Lookup(term)._fetch_lookup(t, cfg.yf_lookup_count)
            ...

normalize -> symbols        (yeni semboller, pasif)   [kapi disi]
             lookup_results                            [kapili]
             lookup_totals                             [kapili]
```

```python
produces = asof_produces(
    "symbols", "lookup_results", "lookup_totals",
    gate=DISCOVERY_GATE_TABLE,
)
```

**Adaptif eşik K6'nın uygulamasıdır.** `yf_lookup_all_threshold` varsayılanı
**500**: §4.1/8'de `BTC` (503) sınırda tam kümesini verirken `GOLD` (7.261)
ve `TECH` (10.000) `all`'da %70'ten fazlasını kaybediyor. Eşik `all`'ın
gözlenen tavanının (~1.000) altında tutulur ki kırpılma **başlamadan**
tipli dala geçilsin.

Sembol döngüsünde tipli dal neredeyse hiç tetiklenmez (AAPL 57, THYAO 1);
maliyet 1 istek/sembol kalır. Geniş serbest terimlerde 8 istek ödenir ve
karşılığında 3–4 kat sembol alınır.

Aynı sembol iki tipte birden dönebilir (§4.1/8, `BTC`'de üç örnek); PK
`(query_term, as_of_date, symbol)` olduğu için son yazan kazanır ve
`lookup_type` kolonu hangi çağrının yazdığını kaydeder (§5.6).

`_fetch_lookup` (ham gövde) kullanılır, `get_all()` (DataFrame) değil:
`Lookup._parse_response` `lookupTotals` ve `total` alanlarını **atar**
(`lookup.py:96-104`), oysa `lookup_totals` tablosu, eksiksizlik kanıtı ve
**adaptif dalın tetikleyicisi** onlara dayanır. `_fetch_lookup`
sarmalayıcının kendi metodudur — K7 korunur.

### 7.3 `screener` — piyasa ekseni, `scope="variant"`, hash kapılı

```
variants(cfg, session) -> screens tablosundan is_enabled olanlar
                          (cfg.yf_screen_keys ile daraltilabilir)

fetch(mctx):  key = mctx.variant
              offset = 0, sayfa = 0
              while sayfa < cfg.yf_screen_max_pages:
                  if sayfa == 0:
                      r = yf.screen(key, count=cfg.yf_screen_size,
                                    sortField=..., sortAsc=...)      # GET
                  else:
                      r = yf.screen(key, offset=offset, size=cfg.yf_screen_size,
                                    sortField=..., sortAsc=...)      # POST
                  #        ^^^^ K12: ikinci sayfadan itibaren `size`
                  quotes += r["quotes"]
                  if not r["quotes"] or offset + len(r["quotes"]) >= r["total"]:
                      break
                  offset += len(r["quotes"]); sayfa += 1

normalize ->  screens          (metadata tazeleme)   [kapi disi]
              symbols          (yeni, pasif)         [kapi disi]
              screen_quotes                          [kapi disi]
              screen_runs      (kapinin kendisi)
              screen_members   (kapinin cocugu, replace_scope)
```

```python
produces = ("screens", "symbols", "screen_quotes", "screen_runs", "screen_members")
```

**`sortField` / `sortAsc` her istekte açıkça verilir** (K15). `yf.screen`'de
`sortAsc` varsayılanı `None` → azalan; sayfalar arası sıra kararlı olmazsa
sayfalar örtüşür ya da sembol atlanır. Değerler `screens` satırından gelir.

**İlk sayfa ayrıcalıklıdır** (§4.1/13): `title`, `description`,
`rawCriteria`, `criteriaMeta`, `id`, `versionId`, `lastUpdated` yalnızca
predefined GET yanıtında var. `screens` metadata'sı ve
`screen_runs.criteria_json` bu sayfadan tazelenir.

**Custom ekranlarda ilk sayfa da POST'tur** ve 5 anahtar döner (ölçüldü) —
metadata gelmez; `screens.title`/`description` `ScreenDef`'ten yazılır ve
tazelenmez.

**Durma koşulu üç dallıdır**: boş sayfa **veya** `offset ≥ total` **veya**
sayfa sınırı. `offset > total` durumunda Yahoo hata vermeden 0 satır
döndürür (§4.1/12).

---

## 8. Veri akışı ve keşif politikası

### 8.1 Sembol tarafı

Mevcut akış hiç değişmeden (`runner.py`): kuyruk, shard, proxy rotasyonu,
backpressure, `yfin_sync` advisory kilidi.

```bash
YF_DISCOVERY_ENABLED=true yfin sync --datasets search,lookup --exchange IST
```

Transaction sınırı (sembol × dataset); bir sembolün `search`'ü patladığında
`lookup`'u yazılmış kalır.

### 8.2 Serbest terim

```bash
YF_DISCOVERY_ENABLED=true yfin discover term "Turkish Airlines"
```

Aynı dataset'ler, aynı tablolar; tek fark `query_term`'ün sembol
olmamasıdır. Terim **64 ASCII karakteri** aşarsa komut reddeder (§5.1).
`discovery_asof_state.query_term`'de FK olmadığı için (K3a) kapı satırı
sorunsuz yazılır.

`sync_run_items.symbol` `VARCHAR(32)` olduğu için 32'yi aşan terim orada
kırpılarak yazılır — burası **denetim** kaydıdır, anahtar değil.

Bu komut sembol döngüsü kurmaz; `market_runner` ölçeğinde tek process
çalışır.

### 8.3 Keşif ve terfi

`is_known` **yazma sırasından türetilmez**, `normalize` içinde hesaplanır:
sembol kodu `SymbolType()` kısıtına uyuyorsa (≤ 32 karakter, saf ASCII) hem
`symbols` yazımına dahil edilir hem `is_known=1` alır; uymuyorsa `symbols`
yazımına girmez ve veri satırı `is_known=0` ile yazılır.

Doğrulama **`^` karakterini kapsamalıdır** (§4.1/16): 9.243 sembolde 93 tanesi
`^` ile başlıyor (endeksler). Dışlayan bir regex endeksleri toptan
reddederdi.

Yeni sembol: `is_active=0`, `discovered_by`, `discovered_at`.
Var olan sembol: yalnız o yolun doldurduğu tanımlayıcı kolonlar tazelenir
(§5.12).

Aktifleştirme **elle**:

```bash
yfin symbols activate --discovered-by screener --exchange IST --dry-run
yfin symbols activate --discovered-by screener --exchange IST
```

`--dry-run` kasıtlıdır: `most_shorted_stocks` tek başına 4.022 sembol
bildiriyor.

**Ekran keşfi (kayıt, otomasyon değil).** `search_lists`'in
`PREDEFINED_SCREENER` satırları, `screens.py`'de tanımlı olmayan onlarca
Yahoo ekran adını bildiriyor (`MS_TECHNOLOGY`, `SEMICONDUCTORS`,
`HIGHEST_DIVIDEND_STOCKS`, …; §4.1/6). Bunlar `yf.screen()`'e anahtar olarak
verilebilir ama **otomatik koşturulmaz**: `screens` tablosuna girmeleri
operatör kararıdır. `yfin screen list --discovered` bunları
`search_lists`'ten okuyup gösterir.

### 8.4 Idempotency ve `as_of_date`

`as_of_date` = koşunun **UTC** günü (`datetime.now(UTC).date()`); yerel saat
diliminden bağımsızdır.

`screen_members` `replace_scope` kapsamı `(screen_key, as_of_date)` olduğu
için **günün son koşusu kazanır**. Ölçüm bunu zorunlu kılıyor: `day_gainers`
`total`'ı iki ölçüm arasında 122 → 117 oynadı.

`screen_quotes` `replace_scope` **değildir**: bir sembolün kotasyonu ekranın
kadrosundan çıkmasıyla silinmez.

`search_quotes` / `lookup_results` düz upsert'tir. Kapsam daraldığında eski
satır kalır; kabul edilen bedeldir çünkü arama sonucu kümesi ekran kadrosu
gibi bir "üyelik" iddiası taşımaz.

### 8.5 `news` seyrek güncelleme

Search yolunun `update_columns`'ı yalnız **doldurduğu** kolonlarla sınırlıdır:

```python
_SEARCH_NEWS_UPDATE = (
    "title", "pub_date", "provider_name", "click_through_url", "content_type",
)
```

Kapsam **dışı**: `summary`, `description`, `canonical_url`, `provider_url`,
`provider_source_id`, `display_time`, `thumbnail_*`, `raw_json`. Bunlar
INSERT'te yazılır, sonraki Search geçişlerinde **dokunulmaz**.

Mevcut `news` dataset'inin tam kapsamlı `_NEWS_UPDATE` listesi
(`datasets/news.py:22`) **değişmez**. Sonuç: zengin gövde daima fakiri ezer,
tersi asla olmaz.

`relatedTickers` → `news_symbols` satırları. `pub_date` `NOT NULL`'dır;
`providerPublishTime` yoksa satır düşer ve uyarı loglanır.

### 8.6 İstek bütçesi

| Koşu | İstek |
|---|---|
| `yfin sync --datasets search` (4.500 sembol) | 4.500 |
| `yfin sync --datasets lookup` (4.500 sembol) | ~4.500 (adaptif dal sembol terimlerinde ~hiç tetiklenmez) |
| `yfin discover term "<geniş terim>" --datasets lookup` | 8 |
| `yfin screen sync` (19 predefined, `max_pages=4`) | 32 |
| `yfin screen sync` (19 predefined, sınırsız) | 49 |
| `yfin screen sync --screens tr_equity` | 3 |

`YF_DISCOVERY_ENABLED=false` varsayılanıyla `yfin sync`'in bugünkü maliyeti
**değişmez** (K11).

### 8.7 Önerilen koşu takvimi

| İş | Sıklık |
|---|---|
| `yfin screen sync` | Günlük, seans kapanışından sonra |
| `yfin sync --datasets search,lookup` (bayrak açık) | Haftalık |
| `yfin symbols activate --dry-run` gözden geçirme | Haftalık, elle |

---

## 9. Hata yönetimi ve veri bütünlüğü

### 9.1 İzolasyon sınırı

| Eksen | Sınır | Bir hücre patladığında |
|---|---|---|
| Sembol | (sembol × dataset) | Komşu dataset ve komşu sembol yazılmış kalır |
| Piyasa | (dataset × ekran) | Komşu ekran yazılmış kalır |

`market_runner._run_turn` her turu kendi transaction'ında çalıştırır.

### 9.2 Durum türetmesi — **tablo bazında**

`_record_items` `WriteStats`'tan **tablo başına bir satır** yazar (S §8.1).
Durum bu yüzden hücrenin tamamına değil, **her hedef tabloya ayrı ayrı**
aittir:

| Durum | Koşul | Ölçülmüş örnek |
|---|---|---|
| `ok` | O tabloya satır yazıldı | |
| `empty` | O tabloya `attempted=0` ve `skipped=0` (AH §7.2) | `"Turkish Airlines"`: `search_quotes` `empty`, `news` `ok` |
| `skipped` | `content_hash` eşit | Ekran kadrosu değişmedi; `screen_quotes` yine `ok` |
| `failed` | İstisna, §9.3-9.4 dahil | |

Örnekler:

- **`zzzqqxnope`**: tüm bloklar boş → `gated` boş, kapı satırı yazılmaz,
  her hedef tablo `empty`.
- **`"Turkish Airlines"`**: `search_quotes` `empty`, `news` ve
  `research_reports` `ok`, kapı satırı `search_report_hits` üzerinden yazılır.
- **`total=0` olan ekran**: `screen_runs` `ok` (başlık satırı yazıldı,
  `total=0`), `screen_members` `empty`, `screen_quotes` `empty`.

Bu, kapı ve başlık satırlarının kendi tablolarının durumunu belirlediği, veri
tablolarının ise ayrı denetlendiği anlamına gelir — `produces` ile fiili çıktı
böylece ayrışmaz.

### 9.3 404 kuralına bilinçli istisna

Proje "HTTP 404 → `empty`" kuralını taşıyor (AH §8.4).

Screener'da **istisna**: `screens` tablosundaki bir anahtar 404 alırsa
`failed` yazılır. Ölçüm: `yf.screen('nosuchscreen')` → `HTTPError: HTTP Error
404`. Bu "veri yok" değil, **yanlış yazılmış ekran adı**dır. SI §8.2'deki
istisnanın kardeşi; ikisi de aynı ilkeye dayanır: **404'ün anlamı sorgulanan
şeyin türüne bağlıdır.**

### 9.4 İstemci tarafı doğrulama

`EquityQuery` / `FundQuery` / `ETFQuery` geçersiz alan ya da değer için
**ağa çıkmadan** `ValueError` fırlatıyor (§4.1/14). Bu bir koşu hatası değil,
**tanım hatası**dır: `screens.py` yüklenirken ortaya çıkar.

`YF_SCREEN_SIZE` Pydantic'te `le=250` ile sınırlanır; yapılandırma hatası
`Settings` yüklenirken görülür, koşu ortasında değil.

### 9.5 Normalizasyon kuralları (yeni)

1. **`ipoExpectedDate` ve `nameChangeDate` ISO metindir**, epoch değil
   (§4.3). `dt` kind'ı kullanılır (`_to_datetime` hem `numbers.Real` hem
   metin kabul eder, `kinds.py:76-88`). `epoch_s` verilseydi ikisi de sessizce
   NULL olurdu.
2. **`prevName` baştaki boşlukla gelebilir** (`' LiveWire Group, Inc.'`).
   `nz.to_str` kırpar.
3. **`corporateActions` listedir**; kolona çıkmaz, `raw_json`'da kalır.
4. **`score` geniş aralıklıdır** (12,2 – 16.067.500,0). `PriceType()` yeterli.
5. **`reportDate` iki biçimlidir** (§4.3): domain yolunda
   `nz.to_datetime_utc`, Search yolunda `epoch_to_datetime(..., unit="ms")`.
6. **Alan seti kotasyon tipine göre VE tip içinde değişir** (§4.1/2);
   eksiklik NULL'dur, hata değil.
7. `lookup_totals` tipleri **yanıttan** okunur, `LOOKUP_TYPES` sabitinden
   değil (§4.1/10).
8. **`quotes` satırı `symbol` taşımıyorsa elenir** (K14, §4.1/3).
9. **`lists` satırı `type`'a göre iki farklı alan setinden birini taşır**
   (§4.1/6); eşlenmeyen alanlar NULL.

### 9.6 Eksiksizlik iddiasının kanıt zinciri

"Tüm data eksiksiz yazıldı" iddiası **dört** bağımsız kayda dayanır:

1. **`screen_runs.total` vs `fetched_rows`** — sayfa sınırına takılan ekran
   görünür.
2. **`lookup_totals.total` vs `lookup_results` satır sayısı** — Lookup
   kırpması görünür. Bu aynı zamanda K6'nın adaptif dalını **tetikleyen**
   sinyaldir (§7.2), yani kayıp yalnız kaydedilmez, azaltılır.
3. **`discovery_asof_state.row_count`** — kapı satırındaki satır sayısı,
   veri tablolarındaki fiili satırla karşılaştırılabilir.
4. **`raw_json` + `warn_unmapped`** — haritalanmayan her kaynak anahtarı
   saklanır ve terfi sinyali loglanır. Veri kaybı yoktur; yalnızca tipli
   kolona çıkmamıştır.

**Dürüst sınır:** `all` çağrısının tavanı ve `yf_screen_max_pages` gerçek
kayıplar üretir. Tasarım bunları gizlemez; 1. ve 2. kayıtlar farkı ölçülebilir
kılar ve `yfin screen list` / `yfin status` raporlar.

### 9.7 Transaction ve eşzamanlılık

Sembol tarafı `yfin_sync`, piyasa tarafı `yfin_market_sync`, domain tarafı
`yfin_domain_sync` kilidini kullanır — üç komut eş zamanlı koşabilir.

`research_reports` **paylaşılan** tablodur: hem domain hem search yolu yazar
ve iki farklı kilit altında koşarlar. Aynı `report_id` için upsert
yarışabilir; `ON DUPLICATE KEY UPDATE` bunu güvenli kılar ve `first_seen_at`
update kapsamı dışında olduğu için ilk yazanın damgası korunur.

`screen_quotes` de paylaşılan sayılır: aynı sembol farklı ekranlardan
yazılabilir. PK `(symbol, as_of_date)`; ekranlar sıralı koştuğu için yarış
yoktur.

---

## 10. Test stratejisi

### 10.1 Fixture'lar

Ölçüm sırasında yakalanan **gerçek** yanıtlar:

```
tests/fixtures/_discovery/
    search_AAPL.json                 # sembollu + Crunchbase sembolsuz satir
    search_GC=F.json                 # lists dolu, prevName + nameChangeDate,
                                     #   ayni tipte farkli alan sayilari
    search_Turkish-Airlines.json     # 0 quote, 8 haber, 3 rapor, 1 nav
    search_zzzqqxnope.json           # tumu bos
    search_BTC-USD.json              # CRYPTOCURRENCY, dar alan seti
    search_lists_two_shapes.json     # ALGO_WATCHLIST + PREDEFINED_SCREENER
    lookup_BTC_all.json              # 9 tipli lookupTotals, privateCompany
    lookup_GOLD_all.json             # KIRPILMIS all (996 / 7261)
    lookup_GOLD_equity.json          # tipli dal ornegi
    lookup_zzzqqxnope.json           # total=0
tests/fixtures/_screen/
    day_gainers_p0.json              # GET yanit, 17 anahtar
    top_mutual_funds_p0.json         # GET, total=1788
    top_mutual_funds_p1.json         # POST, 5 anahtar
    top_mutual_funds_last.json       # offset=1750, 38 satir
    tr_equity_p0.json                # custom POST, 5 anahtar, metadata YOK
    bond_etfs_p0.json                # karisik quoteType (EQUITY + ETF)
```

### 10.2 Unit — ağsız, DB'siz

| Test | Sürdüğü şey |
|---|---|
| `test_search_normalize.py` | Tipe göre değişen alan seti; sektör/endüstri NULL'ları |
| `test_search_crunchbase_filter.py` | **Regresyon:** `symbol` taşımayan satırlar elenir (K14) |
| `test_search_lists_shapes.py` | İki `lists` şekli; `list_key` ayrımı; eşlenmeyen alanlar NULL |
| `test_search_no_quotes.py` | 0 quote + dolu haber → `search_quotes` `empty`, `news` `ok` |
| `test_search_empty.py` | `zzzqqxnope` → kapı satırı **yazılmaz** |
| `test_lookup_adaptive.py` | **Regresyon:** `lookupTotals.all > eşik` → yedi tipli dal; altında tek çağrı (K6) |
| `test_lookup_normalize.py` | 9 tipli `lookupTotals`; `privateCompany`; `source_rank` ≠ `rank` |
| `test_screen_paging.py` | **Regresyon:** ilk sayfa `count`, sonrakiler `size` (K12) |
| `test_screen_sort_explicit.py` | **Regresyon:** her istekte `sortField`+`sortAsc` verilir (K15) |
| `test_screen_stop.py` | Üç durma dalı |
| `test_screen_hash_body.py` | **Regresyon:** kotasyon metrikleri hash gövdesinde yok (K4) |
| `test_screens_valid.py` | Her `ScreenDef` kurulabiliyor; `key` ≤ 32 ASCII |
| `test_screens_match_library.py` | 19 predefined kümesi kütüphaneyle aynı |
| `test_screener_fields.py` | **100 / 75 / 25** bölünmesi; ortak 75'in kolon adları `INFO_FIELDS` ile aynı. Doluluk oranı üzerine **kurulmaz** (§4.5) |
| `test_date_like_strings.py` | `ipoExpectedDate` / `nameChangeDate` ISO metinden `dt`'ye |
| `test_symbol_charset.py` | `^` ile başlayan endeks sembolleri kabul edilir (§8.3) |
| `test_market_scope_variant.py` | `variant` dalı çalışır; `region`/`global` dalları değişmedi; `for_region` `variant`'ı düşürmez |
| `test_merge_stats.py` | `merge_stats` sayaçları toplar; `skipped` iki kez sayılmaz (§6.2.1) |

### 10.3 Repo — gerçek MySQL, ağsız

| Test | Sürdüğü şey |
|---|---|
| `test_discovery_idempotency.py` | İkinci koşu sıfır yeni satır |
| `test_discovery_free_term.py` | **Regresyon:** `symbols`'ta olmayan terim kapı satırı yazabiliyor (K3a — `asof_state` ile `ERROR 1452` verirdi) |
| `test_symbol_promotion.py` | **Regresyon:** aktifleştirilmiş sembol yeniden keşfedilince `is_active=1` kalır (K10) |
| `test_symbol_update_scope.py` | **Regresyon:** `lookup` koşusu `long_name`/`currency`'yi NULL'lamaz (§5.12) |
| `test_news_sparse_update.py` | **Regresyon:** zengin satır + Search fakir satırı → `summary`/`description` korunur |
| `test_search_gate_scope.py` | **Regresyon:** dört tablo kapı dışında; `search_quotes` boş + `research_reports` dolu → `KeyError` yok (§6.2.1) |
| `test_report_fk_order.py` | `research_reports` `search_report_hits`'ten **önce** yazılır (FK) |
| `test_screen_replace_scope.py` | Aynı gün ikinci koşu: eski üye silinir, `screen_quotes` silinmez |
| `test_research_report_shared.py` | Aynı `report_id` domain + search yollarından → tek satır, iki bağ (K8) |
| `test_discovery_schema.py` | On tablo, PK'lar, index'ler, collation'lar; `discovery_asof_state.query_term`'de FK **yok** |
| `test_screen_gate.py` | Hash eşitken `screen_members` atlanır, `screen_quotes` yazılır |
| `test_prune_discovery.py` | §12.2'deki dört budama yolunun her biri |

### 10.4 Live — `-m live`, CI'da kapalı

| Test | Sürdüğü şey |
|---|---|
| `test_live_discovery.py` | `search` + `lookup`, AAPL ve THYAO.IS |
| `test_live_screen.py` | `day_gainers` bir sayfa, `total > 0` |
| `test_live_lookup_narrow.py` | **Dar** terim (`AAPL`): `all` = tipli birleşim, fark 0 |
| `test_live_lookup_broad.py` | **Geniş** terim (`GOLD`): `all` kırpılır, tipli birleşim **kat kat** geniş, fark **iki yönlü** — adaptif dal tetiklenir |
| `test_live_screen_paging.py` | `offset`+`size` ile ikinci sayfa dolu döner (K12) |
| `test_live_search_flags.py` | `include_research=True` olmadan `research` boş, ile dolu (§4.1/5) |

`test_live_lookup_narrow` **ve** `test_live_lookup_broad` birlikte
zorunludur: tasarımın ilk hâli yalnız dar terimle ölçülüp yanlış bir
değişmezi kilitlemişti (Ek A.1/10). Tek başına dar test aynı hatayı yeniden
üretirdi.

LIVE testler `yfin_sync` advisory kilidini paylaştığı için unit/repo ile
paralel koşamaz — mevcut kısıt.

### 10.5 Statik analiz

`ruff` + `mypy --strict`. `SCREENER_QUOTE_FIELDS`'ın ürettiği kolonların
SQLAlchemy modeliyle örtüştüğü `test_screener_fields.py`'de doğrulanır;
`fields.py` ile `models/discovery.py` ayrışamaz (S §6.6 ilkesi).

---

## 11. Kenar durumlar

| Durum | Davranış |
|---|---|
| Sorgu terimi 64 ASCII karakteri aşıyor | CLI reddeder (§5.1) |
| Terim 32'yi aşıyor ama 64'ün altında | Kapı satırı tam yazılır; `sync_run_items.symbol` kırpılır (§8.2) |
| Sembol kodu 32 karakteri aşıyor | `symbols` yazımına girmez, `is_known=0`, veri satırı yine yazılır. Ölçülen max 17 |
| Sembol `^` ile başlıyor (endeks) | Normal kabul; doğrulama `^`'i kapsar (§8.3) |
| `quotes` satırında `symbol` yok | Elenir (K14) |
| `lists` satırı `PREDEFINED_SCREENER` | `list_key` = `canonicalName`; `ALGO_WATCHLIST` alanları NULL |
| `screens` tablosu boş | `variants()` boş liste döner, hiç istek yapılmaz |
| Ekran `total=0` | `screen_runs` `ok`, `screen_members`/`screen_quotes` `empty` (§9.2) |
| Ekran 404 | `failed` (§9.3) |
| Search `quotes` boş, `news` dolu | Tablo bazında: `search_quotes` `empty`, `news` `ok` |
| Search tüm bloklar boş | Kapı satırı yazılmaz, tüm tablolar `empty` |
| `lookupTotals.all` eşiği aşıyor | Yedi tipli dala geçilir (K6, §7.2) |
| `lookupTotals` yeni bir tip bildiriyor | Yazılır; `lookup_type` `String(24)` serbesttir, ENUM değil |
| Aynı sembol iki lookup tipinde | Son yazan kazanır; `lookup_type` hangi çağrı olduğunu kaydeder (§5.6) |
| `relatedTickers` evren dışı sembol taşıyor | `news_symbols`'a `is_known=0` ile yazılır (mevcut davranış) |
| Aynı rapor domain + search yollarından | Tek `research_reports` satırı, iki bağ tablosunda birer satır |
| `pub_date` yok | Haber satırı düşer, uyarı loglanır |
| Sayfa sınırına takılan ekran | `total > fetched_rows`, `yfin screen list` gösterir (§9.6) |
| Gün içinde kadro değişti | `replace_scope`, son koşu kazanır (§8.4) |
| `YF_DISCOVERY_ENABLED=false` iken `--datasets search` | `UnknownDatasetError` — dataset kayıtlı değil. Hata mesajı bayrağı önerir (§13.2) |

---

## 12. Migration ve budama

### 12.1 Migration — iki revision

**Revision 1 — `..._research_reports_rename.py`** (K8, SI katmanı canlı
olduğu için kod değişikliğiyle **aynı** commit'te):

1. `RENAME TABLE domain_research_reports TO research_reports` — MySQL 8'de
   metadata-only.
2. `research_reports` + `author` (`String(128)`), + `report_headline`
   (`String(512)`) — `ALGORITHM=INSTANT`.
3. `domain_report_links` FK'sinin hedef tablo adı güncellenir.
4. §5.4'teki beş dosyadaki tablo adı referansları.

**Revision 2 — `..._search_lookup_screener.py`**:

5. On yeni tablo (§5.15).
6. `symbols` + `discovered_by` (`server_default='manual'`) + `discovered_at`
   — `ALGORITHM=INSTANT`. Mevcut satırlar geriye dönük `manual` etiketlenir.
7. `screens` seed: `screens.py`'deki `ScreenDef` kümesi.

**Neden iki revision:** 7. adım `screens.py`'yi **okur**, yani o dosya
mevcut olmalıdır. Tek revision'da §14'ün 1. adımı 3. adımı beklerdi. Ayrıca
yeniden adlandırma bağımsız olarak geri alınabilir kalır.

`downgrade()` her ikisinde simetriktir.

### 12.2 Budama — dört ayrı yol

`prune_asof` imzası `(session, before, *, dry_run, registry, gate_table,
scope_column)`'dur (`prune.py:185-192`) ve **üçüncü parametrenin adı
`scope_column`'dur**, `key_column` değil. Fonksiyon `AsOfGate` örneklerini
tarar (`if not isinstance(dataset, AsOfGate): continue`) ve kapı tablosunda
`dataset` kolonu bekler.

Bu yüzden **tek bir çağrı yetmez**:

| # | Tablolar | Yol |
|---|---|---|
| 1 | `search_quotes`, `search_lists`, `search_report_hits`, `lookup_results`, `lookup_totals` | `prune_asof(..., registry=SYMBOL_DATASETS, gate_table="discovery_asof_state", scope_column="query_term")`. Beşi de `query_term` + `as_of_date` taşır, kapı `dataset` kolonu taşır → mevcut fonksiyon **olduğu gibi** çalışır. |
| 2 | `screen_members` | **Yeni fonksiyon.** `screener` bir `HashGatedDataset`'tir, `AsOfGate` **değildir** → `prune_asof` onu hiç görmez; ayrıca `screen_runs`'ta `dataset` kolonu **yoktur**. `prune_screens(session, before)` `screen_runs`'ı `(screen_key, as_of_date)` ile gruplayıp son günü korur. |
| 3 | `screen_quotes` | **Kapısız.** `screen_key` kolonu yoktur (§5.11) → `scope_column not in table.c` dalından **sessizce atlanırdı**. Doğrudan `as_of_date < cutoff` ile silinir, sembol başına son gün korunur. |
| 4 | `research_reports` | **Budanmaz.** Paylaşılan varlıktır; hem `domain_report_links` hem `search_report_hits` referans verir. Mevcut yetim-rapor temizliği (`prune.py:238-240`) **iki** bağ tablosunu birden kontrol edecek biçimde genişletilir — bugün yalnız `domain_report_links`'e bakıyor ve genişletilmezse Search'ün bulduğu her raporu yetim sayıp **silerdi**. |

`screens` statik kimliktir, budanmaz.

Saklama süreleri: `YF_PRUNE_DISCOVERY_DAYS` (1. yol),
`YF_PRUNE_SCREEN_DAYS` (2. ve 3. yol).

---

## 13. Yapılandırma ve CLI

### 13.1 Yeni `.env` anahtarları

```bash
# `search` ve `lookup` dataset KAYDINI acar. Varsayilan FALSE cunku
# `Registry.resolve(None)` kayitli her dataset'i dondurur: kosulsuz kayit
# ciplak `yfin sync`e sembol basina IKI istek eklerdi (4.500 sembolde
# +9.000 istek/gun). DIKKAT: bayrak kapaliyken `--datasets search` de
# calismaz (S13.2).
YF_DISCOVERY_ENABLED=false

YF_SEARCH_MAX_RESULTS=10
YF_SEARCH_NEWS_COUNT=5
YF_SEARCH_LISTS_COUNT=10

YF_LOOKUP_COUNT=1000

# `lookupTotals.all` bu degeri asarsa `all` cagrisi KIRPILMIS demektir ve
# yedi tipli dala gecilir (K6). Olculdu: BTC 503 -> `all` tam kume;
# GOLD 7.261 -> `all` 996 belge, tipli birlesim 3.313. Esik `all`in
# gozlenen tavaninin (~1.000) ALTINDA tutulur ki kirpilma BASLAMADAN
# tipli dala gecilsin.
YF_LOOKUP_ALL_THRESHOLD=500

# Yahoo tavani 250; asilirsa yf.screen ValueError firlatir (Pydantic le=250).
YF_SCREEN_SIZE=250

# Ekran basina sayfa ust siniri. 19 predefined: sinirsiz 49 istek, cap=4
# ile 32. En pahali ekran `most_shorted_stocks` (total=4.022, 17 sayfa).
YF_SCREEN_MAX_PAGES=4

# Bos = screens.is_enabled olanlarin hepsi.
YF_SCREEN_KEYS=

YF_PRUNE_DISCOVERY_DAYS=180
YF_PRUNE_SCREEN_DAYS=365
```

### 13.2 Komutlar

Search ve Lookup sembol ekseninde dataset olduğu için **kendi komutlarına
ihtiyaçları yoktur**:

```bash
YF_DISCOVERY_ENABLED=true yfin sync --datasets search,lookup --exchange IST
```

**Bayrak kapalıyken bu komut çalışmaz.** `Registry` dataset'i tanımaz ve
`UnknownDatasetError` fırlatır. Bu, K11'in kabul edilen bedelidir: bayrak
alt sistemin **tamamını** açıp kapatır, yalnız `all` genişlemesini değil.
Hata mesajı bayrağı önerir:

```
bilinmeyen dataset: search. YF_DISCOVERY_ENABLED=true ile acilabilir.
```

Gerçekten yeni olan üç komut:

```bash
YF_DISCOVERY_ENABLED=true yfin discover term "Turkish Airlines"
YF_DISCOVERY_ENABLED=true yfin discover term "lithium" --datasets lookup

yfin screen sync
yfin screen sync --screens day_gainers,tr_equity
yfin screen sync --max-pages 17          # most_shorted_stocks'u tam cek
yfin screen list                          # total vs fetched_rows farkiyla
yfin screen list --discovered             # search_lists'ten kesfedilen ekranlar

yfin symbols activate --discovered-by screener --exchange IST --dry-run
yfin symbols activate --discovered-by screener --exchange IST
```

`yfin screen sync` `yfin_market_sync` kilidini alır.

### 13.3 Çıkış kodları

Mevcut `RunTally.exit_code()` semantiği korunur: 0 = `ok`, 1 = çözülemeyen
sembol var, 2 = `partial`/`failed`.

---

## 14. Uygulama sırası

1. **`screens.py`** (`ScreenDef` + 19 predefined türetmesi + custom tanımlar)
   → `test_screens_valid.py`, `test_screens_match_library.py`. Ağsız, DB'siz;
   migration seed'i bu dosyayı okuyacağı için **önce** gelir.
2. **`merge_stats`** (`datasets/base.py`) → `test_merge_stats.py`. Bağımsız,
   yeni bir yardımcı.
3. **`research_reports` yeniden adlandırması** — Revision 1 + §5.4'teki beş
   dosya + `prune.py` yetim-rapor genişletmesi → mevcut domain testleri yeşil
   kalır. SI katmanı canlı olduğu için bu adım **atomiktir**.
4. **`models/discovery.py` + `SCREENER_QUOTE_FIELDS`** → Revision 2 →
   `test_discovery_schema.py`, `test_screener_fields.py`.
5. **`scope="variant"`** (`market/base.py` `_clone` + `variant` alanı,
   `market_runner.py`) → `test_market_scope_variant.py`. Mevcut altı piyasa
   dataset'inin davranışı birebir korunur.
6. **`market/screener.py`** + `datasets/market/__init__.py` import satırı →
   unit (`test_screen_paging`, `test_screen_sort_explicit`,
   `test_screen_hash_body`, `test_screen_stop`) → repo → live.
7. **`discovery/lookup.py`** + `datasets/__init__.py` import satırı → unit
   (`test_lookup_adaptive`) → repo (`test_discovery_free_term`) → live
   (`test_live_lookup_narrow` **ve** `test_live_lookup_broad`). Lookup
   önce: gövdesi daha basit ve kapı tablosunu tek başına sınar.
8. **`discovery/search.py`** — `_SEARCH_NEWS_UPDATE` dar kapsamı **dahil**
   → unit (`test_search_crunchbase_filter`, `test_search_lists_shapes`) →
   repo (`test_search_gate_scope`, `test_news_sparse_update`,
   `test_report_fk_order`). Haber yazımı bu adımın parçasıdır; ayrı bir adıma
   bırakılamaz çünkü `test_search_gate_scope` ona bağlıdır.
9. **`symbols` terfi politikası** → `test_symbol_promotion.py`,
   `test_symbol_update_scope.py`, `test_symbol_charset.py`.
10. **CLI komutları** + `prune.py`'nin dört budama yolu →
    `test_prune_discovery.py`.

Her adım kendi testleriyle yeşil bırakılır ve **hiçbir adım bir sonrakini
beklemez**.

---

## Ek A — Ölçüm günlüğü

Tüm ölçümler 2026-09-04, `yfinance` 1.7.0, doğrudan bağlantı. "DT" =
bağımsız doğrulama turunda yeniden ölçüldü.

| # | Ölçüm | Sonuç | Nereye dayandırıldı |
|---|---|---|---|
| A1 | `Search` blok yapısı, 9 sorgu | §4.1/1-6 | §5.2, §5.3, §7.1 |
| A2 | `Search.news` uuid ∩ `Ticker.news` id | Kesişim boş değil (aynı kimlik uzayı) | §4.1/7, §8.5 dar upsert kapsamı |
| A3 | Lookup 8 tip × 4 terim, kolon setleri | **DT: özdeş DEĞİL**, üst küme ortak | §4.1/9, §5.6 nullable |
| A4 | `all` vs 7 tipin birleşimi, 4 terim | **DT: dar terimde 0, geniş terimde kat kat fark** | **K6 adaptif**, §7.2, §13.1 eşik |
| A5 | `lookupTotals` içeriği | 9 tip, `privateCompany` dahil, hep 0 | §5.7, §9.5/7 |
| A6 | `count` 25/250/1000 kırpması | Terime göre değişen tavan; `all` ~1.000'de sert | §4.1/8, §9.6/2 |
| A7 | 19 predefined `total` | §4.4 tablosu, 49 / 32 sayfa (DT: aritmetik doğru) | `YF_SCREEN_MAX_PAGES=4` |
| A8 | `offset` + `count` vs `size` | 25 vs 250 satır | K12, `test_screen_paging` |
| A9 | Sayfalama sonu | offset=1750 → 38; offset=9000 → 0, hatasız | §7.3 durma koşulu |
| A10 | GET vs POST yanıt anahtarları | 17 vs 5; **custom ilk sayfa da 5** | §7.3 "ilk sayfa ayrıcalıklı" |
| A11 | 150 kotasyon satırında alan sayımı | 100 farklı; "her satırda" **örneklem bağımlı** (51 / 49) | §4.5, `test_screener_fields` doluluk üzerine kurulmaz |
| A12 | Screener ∩ `INFO_FIELDS` | 75 ortak, 25 yeni (DT doğruladı) | §4.5 |
| A13 | 9.243 sembolde uzunluk/karakter | max 17, ASCII dışı 0, özel `. = - ^ + & _` | §4.1/16, §5.1, §8.3 doğrulama |
| A14 | `region=tr` custom sorgu | `total=628`; **`sortAsc=True` olmadan azalan** | §4.1/15, K15 |
| A15 | Geçersiz alan / bölge / `size=251` | Ağa çıkmadan `ValueError` (DT: ağ bombayla sürüldü) | §9.4 |
| A16 | Geçersiz predefined ad | `HTTPError: HTTP Error 404` | K13, §9.3 |
| A17 | `zzzqqxnope` | Search `count=0`, Lookup `total=0`, hatasız | §9.2 |
| A18 | `cache_get` gövdesi | `return self.get(...)` — takma ad | §4.2/1 |
| A19 | `day_gainers` total sürüklenmesi | 122 → 117 (aynı gün) | §8.4 `replace_scope` |
| A20 | Domain vs Search rapor kimliği | Aynı biçim; **orta segment alfanümerik olabilir** | K8, §5.4 |
| A21 | `ipoExpectedDate` / `nameChangeDate` | ISO tarih metni (DT doğruladı) | §9.5/1 |
| A22 | `prevName` baştaki boşluk | `' LiveWire Group, Inc.'` | §9.5/2 |
| A23 | `customPriceAlertConfidence` | `HIGH`, `LOW` | §4.5 |
| A24 | `screenerFieldResults` | Her sorguda `[]` | §1 kapsam dışı |
| A25 | **DT:** `quotes` sembolsüz satırlar | Crunchbase kayıtları, 4 anahtar | **K14**, §7.1 süzgeç |
| A26 | **DT:** `lists` şekilleri | İki şekil: `ALGO_WATCHLIST` (12) / `PREDEFINED_SCREENER` (9) | §5.3 iki şekilli tablo, §8.3 ekran keşfi |
| A27 | **DT:** `include_research` varsayılanı | `False`; kapalıyken `research` hep boş | §4.1/5, §7.1 açık bayrak |
| A28 | **DT:** `total` ≠ `lookupTotals.all` | `SpaceX`: 22 vs 72 | §4.1/11, §9.6/2 |

### A.1 Tasarım sırasında ve denetimde düzeltilen hatalar

1. **"Dördüncü eksen gerekli."** İlk taslak `QueryContext` +
   `QUERY_DATASETS` + `query_runner.py` öngörüyordu. Sembol ekseninin
   `ctx.symbol`'ünün sorgu terimi olarak kullanılabileceği fark edilince
   ~400 satır makine düştü (K1).
2. **"`screen_members` kotasyonu da taşısın."** Aynı sembol beş ekranda
   görünüyorsa 98 alanı beş kez yazılırdı → `screen_quotes` ayrıldı (K5).
3. **"Kotasyon metrikleri hash gövdesine girsin."** Fiyat her gün oynadığı
   için hash hiçbir zaman eşitlenmez, `skipped` hiç üretilmezdi (K4).
4. **"Search haberi mevcut `news` upsert'iyle yazılsın."** Aynı PK, dar
   gövde; kör upsert `summary`/`description`'ı NULL'lardı (§8.5).
5. **"Search reports için ayrı tablo."** Kimlik uzayı tek (K8).
6. **"Keşfedilen sembol `is_active` güncellensin."** Elle aktifleştirileni
   ertesi gün pasife döndürürdü (K10).
7. **"`ipoExpectedDate` epoch'tur."** ISO metin; `epoch_s` sessiz NULL
   üretecekti (§9.5/1).
8. **"`symbols` yazımı `writes` listesinin başına konur."** `AsOfGate._gate_write`
   `_first_row`'dan `as_of_date`/`fetched_at` okuyor ve `symbols`'ta bu
   kolonlar yok. Sıralama disiplini de çözmezdi: `search_quotes` boşken
   `news` dolu olabiliyor. Kapı kapsamı açıkça bölündü (§6.2.1).
9. **DENETİM — "üç tablo kapı dışında."** `research_reports` de `query_term`
   taşımıyor ve `"Turkish Airlines"` senaryosunda ilk dolu write **o** olur.
   8. maddedeki hatanın dördüncü tabloda tekrarıydı. `UNGATED_TABLES` dörde
   çıkarıldı.
10. **DENETİM — "`type=all` yedi tipin birleşimini verir."** Tasarımın
    **çekirdek maliyet kararıydı** ve tek bir dar terimle (`BTC`, toplam 503)
    ölçülüp genellenmişti. Geniş terimlerde `all` ~1.000'de kırpılıyor:
    `GOLD` → birleşim 3.313, `all` 996, fark **iki yönde** (354 / 2.671);
    `TECH` → 4.024 vs 998. K6 adaptif hâle getirildi, `lookup_type` kolonu
    geri kondu, `test_live_lookup_broad` eklendi. **Tek bir örnekle
    genelleme yapmanın bedeli buydu.**
11. **DENETİM — `asof_state` kullanılamaz.** Kolon `symbol_fk_column` ile
    tanımlı, yani `symbols.symbol`'a FK taşıyor. Serbest terim kapı satırı
    `ERROR 1452` alırdı. `discovery_asof_state` açıldı (K3a) — SI'nin
    `domain_asof_state` için verdiği kararın aynısı.
12. **DENETİM — SI katmanı canlı.** §0 "domain dataset'leri henüz yok"
    diyordu; beş dataset kayıtlı ve `domain_runner.py` koşuyor. K8 yeniden
    adlandırmasının etki alanı beş dosyaya yayılıyor ve migration ile aynı
    değişiklikte gitmesi gerekiyor (§5.4, §12.1).
13. **DENETİM — `MarketContext`'te `variant` alanı yok.** `for_variant`
    varyantı hiçbir yere yazamazdı; `fetch` hangi ekranı çekeceğini
    öğrenemezdi. Alan eklendi ve `for_region`'ın elle kopyalaması `_clone`'a
    çekildi (§6.1) — yoksa yeni alan bölge dalında sessizce düşerdi.
14. **DENETİM — `variants(settings)` DB okuyamaz.** `Settings` bir Pydantic
    nesnesidir. İmza `session` alacak biçimde düzeltildi (§6.1).
15. **DENETİM — budama çalışmazdı.** `screener` `AsOfGate` değil,
    `screen_runs`'ta `dataset` kolonu yok, `screen_quotes`'ta `screen_key`
    yok, `search_lists`/`lookup_totals` `symbol` taşımıyor. Tek `prune_asof`
    çağrısı üç tabloyu **sessizce** atlar, birini yanlış kapsamla budardı.
    §12.2 dört ayrı yola bölündü. Ayrıca mevcut yetim-rapor temizliği
    genişletilmezse Search'ün bulduğu her raporu **silerdi**.
16. **DENETİM — kayıt import zinciri eksik.** Modül düzeyi `register()`
    yalnız import edilirse koşar; `datasets/__init__.py` ve
    `datasets/market/__init__.py` import satırları yazılmamıştı → dataset'ler
    hiç kaydolmaz, `yfin screen sync` sessizce sıfır iş yapardı (§6.4).
17. **DENETİM — `merge_stats` yok.** Kod tabanında karşılığı olmayan bir
    yardımcı kullanılmıştı; ayrıca `skipped` iki kez sayılıyordu ve `gated`
    satırsız + `skipped` doluyken `_first_row` `ValueError` fırlatıp hücreyi
    `failed`'e düşürürdü (§6.2.1, §14/2).
18. **DENETİM — `symbols` ortak update kapsamı.** `lookup` yolu
    `long_name`/`currency`/`timezone` döndürmüyor; ortak listeyle her koşuda
    `search`'ün yazdığını NULL'lardı. Yola özgü kapsam tanımlandı (§5.12).
19. **DENETİM — sembolsüz `quotes` satırları.** `include_cb=True` varsayılanı
    Crunchbase kayıtları döndürüyor ve `.response` bunları süzmüyor; kör
    `q["symbol"]` `KeyError` verirdi (K14).
20. **DENETİM — `lists` tek şekilli sanılmıştı.** İki şekil var ve
    `PREDEFINED_SCREENER` şekli 19'un dışında düzinelerce ekran adı
    bildiriyor. "Yalnız sayı var" gerekçesi düzeltildi; kapsam dışı bırakma
    sebebi **maliyet** olarak yeniden yazıldı (§1, §5.3, §8.3).
21. **DENETİM — `include_research` varsayılanı `False`.** Ölçümler bayrak
    açık yapılmış ama doküman bunu söylemiyordu; §7.1 varsayılan çağrıyı
    kullansaydı iki tablo **hiç** dolmazdı (§4.1/5).
22. **DENETİM — sıralama belirsizdi.** `sortAsc` varsayılanı azalan;
    sayfalar arası sıra kararlı olmazsa sayfalar örtüşür ya da sembol
    atlanır (K15).
23. **DENETİM — sembol karakter kümesi eksikti.** İlk örneklemde `^` hiç
    yoktu; 9.243 sembolde 93 endeks sembolü `^` ile başlıyor. Dışlayan bir
    doğrulama endeksleri toptan reddederdi (§4.1/16, §8.3).
24. **DENETİM — `rank` üç farklı anlamda kullanılmıştı.** Lookup'ın kaynak
    `rank` alanı bir sıra değil skordur; `source_rank` olarak ayrıldı ve
    `rank` semantiği tek yerde tanımlandı (§5.14).
25. **DENETİM — durum türetmesi çelişkiliydi.** "`screen_runs` yazılır" +
    "hücre `empty`" aynı anda söyleniyordu. Durum `_record_items` tarafından
    **tablo bazında** türetilir; §9.2 buna göre yeniden yazıldı.
26. **DENETİM — `ScreenDef` eksik alanlıydı.** `title`, `description`,
    `is_enabled`, `sort_field`, `sort_asc` üç ayrı bölümde kullanılıyor ama
    dataclass'ta yoktu (§6.5).
27. **DENETİM — migration `screens.py`'yi bekliyordu.** Seed adımı o dosyayı
    okuyor ama uygulama sırasında dosya sonra yazılıyordu; "hiçbir adım bir
    sonrakini beklemez" iddiası bozuluyordu. İki revision'a bölündü
    (§12.1, §14).
