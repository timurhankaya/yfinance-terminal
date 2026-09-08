# Web terminal faz 2c: görsel katman

Status: partially implemented, 2026-09-08 (2c-1 primitives and the
sparkline column; 2c-2 `HEAT` and `COMP`. 2c-3, the charts inside `FA`,
`ANR`, `HDS` and `CA`, is still open -- see "Uygulama sırası")
Date: 2026-09-08
Revised 2026-09-08 after three independent reviews (see "Revizyonlar").
`2026-09-07-web-terminal-design.md`'nin çocuğu;
`2026-09-08-web-terminal-faz2a-layout-design.md` ile kardeş. İkisinin
kesiştiği üç nokta "Kardeş spec ile kesişme"de sayılıdır.

## Neden

Faz 1e "arşivdeki her şey terminalden okunur" sözünü tuttu: 61
dataset'in hepsi bir panelden görülebiliyor. Ama `GP`/`GIP` dışında
hepsi **tablo** — sektör metrikleri, screen rosterleri, gelir tabloları,
hedef fiyatlar, sahiplik dağılımları sayı ızgarası olarak duruyor.

Bu bir kapsam eksiği değil, sunum eksiği: veri zaten diskte. Bu spec
hiçbir yeni fetch, hiçbir yeni tablo, hiçbir Yahoo çağrısı getirmez.
Yalnızca yazılı olanı çizer.

**Parent'tan yetki.** Parent spec'in "Faz 2" maddesi yerleşim, gruplar,
sayfalar, DB kalıcılığı, replay ve hosted kullanıcıları sayıyor;
**görsel katman o listede yok**. Bu spec parent'ı genişletir, bölmez;
parent'a bu turda bir "Revizyonlar" girdisi eklenir.

### Rakip analizi, 2026-09-08

| Ürün | Bağlama | İmza görselleri | Alınan ders |
| --- | --- | --- | --- |
| Bloomberg Terminal / Launchpad | grup harfleri | `COMP`, sektör ısı haritası, `MOST`, `WEI` | `COMP` ve ısı haritası bizde yok; ikisi de mevcut veriden çizilebiliyor |
| Koyfin | 7 renk grubu | ısı haritaları, renk kodlu performans matrisleri, kaydedilmiş tablo şablonları | Gruba renk de ver: harf kimlik, renk sinyal. Tablo şablonu fikri `screen_quotes`'un 107 kolonu için not edildi (kapsam dışı) |
| OpenBB Workspace | parametre adına göre | widget başına tablo/grafik katmanı | Bağlamayı parametre adından türetmek bizde gereksiz: panel sembolü zaten prop alıyor |
| TradingView | layout sync | bar replay, screener ısı haritası | Replay ayrı bir faz 2 maddesi; ısı haritası buraya |
| Finviz | yok | treemap ısı haritasının kendisi ürün | Tek görünüm bir ürünü taşıyabiliyor; bizde `domains` ve `screen_quotes` ile mümkün |

Kaynaklar: docs.openbb.co/workspace, koyfin.com/features/custom-dashboards.

Bu ürünlerin hiçbirinde **kendi arşivinin sağlığı** bir görünüm değil.
Bizde `bar_gaps` var; `COV` (kapsama ısı haritası) bu spec'in dışında
ama not edilmiştir.

### Mevcut durum, 2026-09-08'de doğrulandı

| Gerçek | Yer |
| --- | --- |
| `lightweight-charts` ^5.2.1 kurulu; ona dokunan **tek** dosya `Chart.tsx` | `web/package.json:16`, `web/src/panels/Chart.tsx:1,24,32` |
| `Chart.tsx` renklerini stylesheet'ten okuyor (`getComputedStyle` + fallback), "grafik terminalin geri kalanından ayrışmasın" diye | `web/src/panels/Chart.tsx:36-49` |
| Tablo motorunun kolon tipi `Column<Row> { key, label, align?, format? }`; `DatasetTableProps` = `{ columns, rows, hide?, reverse?, truncated?, links?, onLoadMore?, loadingMore? }` | `web/src/panels/common.tsx:180-185`, `web/src/panels/table.tsx:158-176` |
| `DatasetTable`'ı kullananlar: `dataset.tsx`, `DES`, `PX`, `CA`. `WLA` kendi `<td>`'lerini yazıyor; `EQS` alt seviye `DataTable`'ı kendi `Column[]`'ı ile kullanıyor | `web/src/panels/WLA.tsx:79-92`, `EQS.tsx:233-277` |
| Katalogdan gelen tek daimi gizli kolon `raw_json`; paneller ayrıca `hide` ile `symbol`'ü düşürüyor | `table.tsx:14`, `CA.tsx:29`, `PX.tsx:75`, `dataset.tsx:118` |
| Günlük kapanışlar `price_history`'de, PK `(symbol, session_date)`; `price_bars` intraday, `periodic_bars` 1wk/1mo | `models/prices.py:19-39`, `models/bars.py:99-102,113,195` |
| `/ui/api` rotaları `Collection[T]` / `Resource[T]` zarfı döndürüyor (`data`, `next_cursor`, `as_of`) ve router `include_in_schema=False` | `ui/data.py:49,104,121,159`, `api/schemas/common.py:23-42` |
| `/ui/api/screens/{key}` üye başına `price`, `change`, `change_percent`, `volume`, `market_cap`, `fifty_two_week_change_percent` döndürüyor | `ui/data.py:320-326,463-470` |
| `domain_metrics`: `market_cap`, `market_weight`, `reg_market_change_pct`, `ytd_change_pct`, `one_year/three_year/five_year_change_pct` | `models/domains.py:182,184,194-198` |
| `analyst_price_targets`: low/high/mean/median/current | `models/analysis.py:113-120` |
| `analyst_recommendations` ölçülen satır sayısı 3-4; `period` göreli dönem anahtarı | `models/analysis.py:54-55,63` |
| CSP `style-src 'self'`, nonce yok; `web/src` altında hiç `style=` yok | `ui/pages.py:26-30`, grep |
| Yön renkleri CSS'te **çıplak hex**, değişken değil: `.strip-change.up { #4cc38a }`, `.down { #ff6b6b }` | `web/src/app/styles.css:76-77,89-90` |
| Sayılar sabit `en-US`, tarih-saatler UTC; UI dizeleri İngilizce | `web/src/panels/format.ts:11`, `table.tsx` |
| `RequestBrake` `/ui/api`'nin tamamını kapsıyor (IP başına 600 rpm) | `ui/public.py:59,76`, `api/core/config.py:70` |
| Tick geldiğinde yalnız o satırın render edildiği zaten test edilmiş | `web/src/live/hooks.test.tsx:78-92` |
| `store.bench.ts` ölçüm kalıbı var; 200 sembol karede 0,024 ms | `web/src/live/store.bench.ts`, `docs/measurements/websocket.md:537,541-553` |
| 61 dataset (49 symbol + 7 market + 5 domain) | `datasets/registry.py`, sayılarak |

## Kararlar

1. **Yeni grafik bağımlılığı yok.** `web/src/panels/viz/` altında beş
   SVG primitifi yazılır. Reddedilenler: **ECharts** kendi
   tipografisini, temasını ve tooltip'ini getirir ve ~1 MB'tır;
   **Recharts** hücre başına bizim `Sparkline`'ımızdan çok daha derin
   bir ağaç kurar ve kendi tema kancalarını dayatır; **visx** d3
   modüllerini sürükler; **`lightweight-charts`'ı sparkline olarak
   kullanmak** hücre başına bir chart instance'ı ve bir canvas açar.
   Karşılaştırma dürüst olsun diye: bizim primitiflerimiz de birer React
   bileşenidir — fark ağacın derinliği ve `React.memo` disiplinidir,
   kategorik bir fark değil.
2. **`lightweight-charts` zaman serisinde kalır** ve ona dokunan tek
   dosya `Chart.tsx` olmayı sürdürür. Viz primitifleri onun
   karşılamadığı türler içindir: treemap, kategorik bar, bullet,
   scatter, hücre içi sparkline.
3. **Grafik tabloyu değiştirmez, üstüne gelir.** `FA`, `ANR`, `HDS`,
   `CA` grafiklerini alır ama sayısal tabloları kalır: bir grafiğin
   okunamayan tek şeyi kesin değeridir.
4. **Renk tek kaynaktan gelir ve o kaynak CSS'tir.** `styles.css`'e
   `--up`, `--down`, `--group-a` … `--group-g` custom property'leri
   eklenir (bugünkü çıplak hex'ler bunlara çıkarılır);
   `viz/colors.ts` onları `Chart.tsx:36-49`'daki `theme()` kalıbıyla
   okur. TS'te ikinci bir renk tanımı **yoktur** — reponun "çift yol
   yok" kuralı buna da uygulanır.
5. **Renk üç role ayrılır ve roller karışmaz.** Yön: `--up`/`--down`.
   Vurgu ve odak: `--accent`. Kimlik: yedi renklik grup paleti.
   `COMP`'un serileri **kimlik rolündedir ve grup paletini kullanır**;
   ayrı bir `CATEGORICAL` palet yoktur (sekizinci renk ayırt
   edilemediği için `COMP` de en çok yedi sembol alır).
6. **Renk SVG'ye `fill`/`stroke` sunum öznitelikleriyle verilir.**
   `style={{ fill }}` yazılmaz: `web/src`'de `style=` yasaktır ve CSP
   `style-src 'self'` inline stil özniteliğini bloklar. Sürekli ölçekli
   treemap dolgusu sınıfla verilemeyeceği için bu kural yazılıdır.
7. **Isı haritası ölçeği ıraksaktır, sıfırda nötrdür ve her zaman bir
   ölçek açıklaması (legend) ile gelir.** Renk tek başına değer
   taşımaz: sayı hücrenin içinde (yer varsa) ve satır detayında yazılı.
8. **Erişilebilirlik iki kalıba ayrılır.** Tıklanamayan grafik
   (`Sparkline`, `Bars`, `Bullet`, `Scatter`) `role="img"` + özet
   `aria-label` taşır — bu yeni bir karar değil, `Chart.tsx:176`'nın
   uzatılmasıdır. **Tıklanabilir grafik (`HEAT`) `role="img"`
   taşımaz**: `role="img"` alt ağacı erişilebilirlik ağacından düşürür
   ve kutular ekran okuyucuya görünmez olurdu. `HEAT`'in `<svg>`'si
   `role="group"` + `aria-label`, her kutu `<g tabindex="0"
   role="button" aria-label="…">`.
9. **Toplu sparkline için tek yeni rota.** 200 sembollük bir `WLA` için
   200 REST çağrısı kabul edilemez; `/ui/api/sparklines` tek çağrıda
   son N günlük kapanışı verir. Bu `/ui/api`'nin **altıncı** rotası
   (`news`, `ticks`, `gaps`, `screens`, `screens/{key}`'den sonra);
   `/v1` sözleşmesi değişmez.
10. **SVG varsayılan; treemap'te hücre sınırı 400.** Fazlası tek bir
    "diğer" kutusunda toplanır ve panel bunu yazar. Canvas'a geçiş
    ölçüm görmeden yapılmaz.

## Mimari

```
web/src/panels/viz/
  scale.ts        linearScale, extent, niceTicks, normalize100
  colors.ts       theme() kalıbıyla CSS'ten okunan yön/grup renkleri,
                  divergingHeat(pct) -> renk
  Sparkline.tsx   tek seri, eksensiz, opsiyonel alan dolgusu
  Bars.tsx        kategorik bar/grup bar + opsiyonel ikinci eksen çizgisi
  Bullet.tsx      aralık (low..high) + ortalama işareti + gerçekleşen
  Treemap.tsx     squarified yerleşim, ıraksak renk, etiket eşiği
  Scatter.tsx     iki eksen + opsiyonel referans çizgisi
  index.ts
```

Her primitif saf bir bileşendir: veri prop'u alır, SVG döndürür, veri
çekmez, store'a bakmaz, `PanelSpec` bilmez. Yerleşim hesapları
(`squarify`, `linearScale`, `normalize100`) ayrı saf fonksiyonlardır ve
doğrudan test edilir. Boyut CSS'in işidir: primitifler `viewBox` +
`preserveAspectRatio` kullanır, `width`/`height` yazmaz.

### Sparkline kolonu üç panelde üç yoldan gelir

Tek bir mekanizma uydurmak yerine her panelin bugünkü yazım biçimi
korunur — üçü de aynı `Sparkline` bileşenini çizer:

| Panel | Bugünkü yazım | Eklenen |
| --- | --- | --- |
| `WLA` | kendi `<tr>/<td>` işaretlemesi (`WLA.tsx:79-92`) | bir `<td>` daha |
| `EQS` | kendi `Column<ScreenRow>[]` dizisi (`EQS.tsx:233-277`) | dizide bir girdi daha |
| `SCR` ve `DatasetTable` yolundaki paneller | `Tab` → `tabbedPanel` → `DatasetView` → `DatasetTable` | `DatasetTable`'a `extra?: Column<Row>[]`; aynı alan `Tab` ve `DatasetView` üzerinden geçirilir |

Motorun kendi tip adları kullanılır (`label`, `format`), yeni bir isim
seti (`header`, `render`) uydurulmaz. `PanelSpec`'e alan **eklenmez**:
parent'ın faz 1 garantisi "`PanelSpec` yerleşim bilgisi taşımaz"
kolon listesini de kapsar.

`HDS` ve `MKT` gibi `tabbedPanel` ile üretilen paneller "kod değil,
konfigürasyon"dur (`curated.tsx:1-3`); grafikleri de konfigürasyonla
gelir: `Tab` tipine opsiyonel bir `chart` alanı eklenir. Elle yazılmış
panele çevrilmezler.

## Yeni mnemonikler

| Kod | Şablon | Panel | Kaynak |
| --- | --- | --- | --- |
| `HEAT` | single | Isı haritası. `HEAT` sektörler; `HEAT <screen_key>` bir screen rosteri. Alan = piyasa değeri, renk = seçilen dönemin değişimi. `needsSymbol=false` | `/ui/api/v1/datasets/domain_metrics`, `.../domains`, `/ui/api/screens/{key}` |
| `COMP` | **single** | Çoklu sembol karşılaştırma. `COMP AAPL MSFT NVDA [1y]`; başlangıç 100'e normalize günlük kapanışlar; **en çok 7 sembol** (grup paleti yedi renk) | `/ui/api/v1/symbols/{s}/bars?interval=1d` |

**`COMP` `single`'dır, `headed` değil.** Parent'ın routing revizyonu
sembolsüz bir market sayfasının paylaşılan linkinin kimsenin sembolünü
taşımadığını yazıyor; `headed` olsaydı sembolsüz bir canlı fiyat şeridi
çizerdi. Emsal `WLA`: aynı şekil, `needsSymbol: false`,
`Layout.Single` (`WLA.tsx:175-176`).

**Dönem enum'dur** (repo kuralı: ayırıcı değer enum):

```ts
enum HeatPeriod { Day = "1d", Ytd = "ytd", Year = "1y",
                  ThreeYear = "3y", FiveYear = "5y", Week52 = "52w" }
enum CompPeriod { SixMonth = "6m", Year = "1y", ThreeYear = "3y" }
```

| Kaynak | Desteklenen `HeatPeriod` |
| --- | --- |
| `domain_metrics` (sektör) | `1d`, `ytd`, `1y`, `3y`, `5y` |
| `/ui/api/screens/{key}` | `1d`, `52w` |

Panel desteklenmeyen dönemi sunmaz ve nedenini yazar.

**`CompPeriod` neden `5y` ve `max` taşımıyor:** `/v1/.../bars` sayfalı
ve UI sayfa tavanı 1000 satır; 5 yıllık günlük ≈ 1.250 satır, yedi
sembolde her biri iki sayfa. Üç yıllık pencere karşılaştırma sorusunu
zaten cevaplıyor; çok sayfalı bir okuma bunun için ödenmez.

**Bilinen dilbilgisi sınırı.** Parser'ın "iki token da mnemonikse ilki
sembol sayılır" kuralı (`parser.ts:93,99-101`) yüzünden `COMP CF MSFT`
`CF` panelini `COMP` sembolünde açar. Bu yeni bir kusur değil: `WLA CF
MSFT` bugün de aynı şeyi yapıyor. Kaçış yolu palet ve URL
(`?symbols=CF,MSFT`); gramerin düzeltilmesi bu spec'in dışındadır ve
`kalan-isler`'e not edilir.

**`HEAT` Enter davranışı.** Sektör kutusunda Enter `DOM`'un `metrics`
sekmesini `domain_key` filtresiyle açar; screen kutusunda `DES`'i
sembolüyle açar. İkisi de `useGo` iledir ve sembol taşıma biçimi
`commands/go.ts`'in bugünkü kalıbıdır (bağlam sembolü history
entry'sinde). Bu çağrıların panel bağlamına taşınması **faz 2a'nın
işidir**; 2c tek panelli dünyada bugünkü `useGo` ile yazılır.

## Zenginleşen paneller

| Panel | Eklenen görsel | Kaynak | Not |
| --- | --- | --- | --- |
| `FA` | Gelir ve net kâr grup barları + net marj çizgisi | `financials` | Yıllık/çeyrek sekmesini izler |
| `ANR` | Hedef fiyat bullet'ı (low/mean/high, işaret son fiyat) + tavsiye dağılımı barı | `analyst_price_targets`, `analyst_recommendations` | Tavsiye tablosu 3-4 satır; grafik de 3-4 çubuktur |
| `HDS` | Top-N kurumsal sahiplik barı + insider net akışı | `institutional_holders`, `insider_transactions` | `Tab.chart` konfigürasyonuyla |
| `CA` | Temettü geçmişi barı; split'ler işaret | `/ui/api/v1/symbols/{s}/actions` | |
| `WLA`, `EQS`, `SCR` | 30 noktalı sparkline kolonu | `/ui/api/sparklines` | |

**`MKT` bu partide yok.** `market_summary_history` günlük kapanış
serisi değil, fetch başına snapshot geçmişidir (anahtar
`(region, board_code)`, sıra `fetched_at`, `datasets/market/status.py:159-183`):
nokta aralığı sync cadence'ine bağlıdır ve bir sparkline'ın vaat ettiği
"son 30 gün"ü vermez. Endeks kartları ayrıca `market_summary`
sekmesinden, seri `summaryhist`ten gelir — iki kaynak. Kendi turunu
hak ediyor.

## `/ui/api/sparklines`

```
GET /ui/api/sparklines?symbols=AAPL,MSFT&points=30
->  Resource[SparklineSet]:
    { "data": { "points": 30,
                "series": [ { "symbol": "AAPL",
                              "closes": ["232.51", "231.90", ...],
                              "first_date": "2026-07-28",
                              "last_date": "2026-09-08" } ],
                "missing": ["ZZZZ"] },
      "as_of": null }
```

- **Zarf `Resource[T]`**, `/ui/api`'nin geri kalanıyla aynı
  (`ui/data.py:104,121`). `as_of` `null`'dur ve gerekçesi fiyat
  tablolarınınkiyle aynıdır (`api/schemas/common.py:29-36`): bir bar
  serisinin "kaynağa karşı en son ne zaman doğrulandı"sı satır başına
  değişir, zarf başına değil.
- **`points`, `days` değil:** son N **işlem günü** (satır sayısı).
  Takvim günü olsaydı `points=30` ~21 kapanış döndürürdü ve
  sparkline'ın uzunluğu sembole göre değişirdi.
- Yalnız `interval=1d`; başka aralık kabul edilmez (sparkline günlük
  bir şekildir; intraday `PX`'in işidir).
- `symbols` en çok 200 (WS bağlantı sınırıyla aynı sayı ve aynı
  gerekçe); `points` 5-90, varsayılan 30. Aşımda 422.
- Arşivde barı olmayan sembol `missing`'e girer; panel o hücreye
  "veri yok" çizer.
- Ondalıklar `paging.to_number` ile (terminalin geri kalanıyla aynı
  dize).
- Sorgu tek: `SELECT symbol, session_date, close FROM price_history
  WHERE symbol = ANY(:symbols) AND session_date >= :since ORDER BY
  symbol, session_date` — PK öneki `(symbol, session_date)` taranır;
  gruplama süreç içinde. `price_bars` **kullanılmaz**, o tablo
  intraday'e ayrılmıştır (`models/bars.py:99-102`).
- `since` en son işlem gününden geriye `points` satır alacak biçimde
  hesaplanır (takvim değil, satır sayarak: `points * 2` takvim günü
  penceresi alınır ve süreç içinde son `points` satır kesilir; tatil
  yoğun bir pencerede bile yeter).
- Fren: mevcut `RequestBrake` (`ui/public.py:59`) istek sayar; bu rota
  tek istekte 6.000 satıra kadar okuyabildiği için sınırlar
  (`symbols`, `points`) frenin değil rotanın kendi korumasıdır.

**İstemci.** `web/src/api/client.ts`'e `getSparklines(symbols, points)`
eklenir (kalıp: `getScreen`, `getBarsWindow`). Panel onu tek bir
`usePanelData` çağrısıyla okur ve satırlara `symbol` anahtarıyla
dağıtır; cache anahtarı `symbols.join(",") + ":" + points`. Sembol
listesi değişince yeniden sorgulanır.

## Hata yönetimi

- Dataset boş → "veri yok" kartı, grafik çizilmez.
- `HEAT`'te tek sektör → treemap tek kutu çizer (geçerli durum).
- `COMP`'ta bir sembolün barı yok → o seri çizilmez, adı "veri yok"
  olarak listelenir; `normalize100` boş seride çağrılmaz.
- Sparkline REST hatası (500/timeout) → kolon başlığında bir kez
  "sparkline yüklenemedi" ve hücreler boş; **satırın kendisi
  etkilenmez** (fiyat hücreleri WS'ten gelir).
- `COMP` 7'den fazla sembol → **komut reddedilir** (emsal:
  `WLA.tsx:59` `throw new Error(WLA_USAGE)`). URL'den geldiğinde
  emsal yine `WLA`'nındır: sessiz `slice(0, 7)` ve panelde bir not.
- `HEAT` bilinmeyen screen anahtarı → uyarı ve `EQS` önerisi.

## Sıralama ve biçim

- `HEAT` kutu sırası treemap'in kendi sırasıdır (değere göre azalan);
  `COMP` seri sırası argüman sırasıdır (renk ataması da öyle).
- Sparkline kolonu **sıralanabilir değildir**: skaler bir değeri yok.
- `closes` dizesi SVG'den önce `asNumber` ile sayıya çevrilir
  (`panels/format.ts`).
- Sparkline normalize **edilmez** (tek sembolün kendi ölçeği);
  `COMP` normalize edilir. Bu yüzden farklı para birimleri sparkline
  kolonunda yan yana durabilir, `COMP`'ta ise yüzde karşılaştırılır.

## Ölçümler

`viz/treemap.bench.ts`, `store.bench.ts` kalıbıyla: 50 / 200 / 400
hücrede `squarify` + render maliyeti. 400 hücre sınırının ve canvas
kararının dayanağı bu ölçüm olur; sonucu `docs/measurements/` altına
yazılır. `WLA` sayfası için ikinci ölçüm: 200 sparkline mount maliyeti
ve bir tick sonrası render sayısı (beklenen: sparkline'lar 0).

## Dosyalar

**Yeni:** `web/src/panels/viz/{scale,colors,Sparkline,Bars,Bullet,Treemap,Scatter,index}.tsx|ts`,
`web/src/panels/HEAT.tsx`, `web/src/panels/COMP.tsx`,
`web/src/panels/viz/*.test.ts(x)`, `web/src/panels/viz/treemap.bench.ts`,
`tests/unit/test_ui_sparklines.py`, `tests/repo/test_ui_sparklines_repo.py`.

**Değişen:** `web/src/app/styles.css` (`--up`, `--down`,
`--group-a…g`; bugünkü çıplak hex'ler bu değişkenlere çıkarılır),
`web/src/panels/common.tsx` ve `table.tsx` (`extra?: Column<Row>[]`),
`web/src/panels/curated.tsx` ve `dataset.tsx` (`Tab.chart`, `extra`
geçişi), `web/src/panels/{WLA,EQS,FA,ANR,CA}.tsx`,
`web/src/panels/index.ts` (iki yeni mnemonik),
`web/src/api/client.ts` (`getSparklines`), `src/yfin/ui/data.py`
(sparklines rotası), `web/src/panels/HELP.tsx`.

**Bağımlılıklar:** yok. Karar 1.

## Testler

**vitest, saf:** `linearScale` uçları; `niceTicks`; `normalize100`
(ilk nokta 100, boş/sıfır seride çağrılmaz); `squarify` alan koruması
ve kutunun içinde kalma; **400 hücre sınırı ve "diğer" kutusu**;
`divergingHeat` sıfırda nötr.

**vitest, bileşen:** her primitifin erişilebilirlik kalıbı (Karar 8:
`Sparkline` `role="img"`, `HEAT` `role="group"` + odaklanabilir
kutular); `Treemap` etiket eşiğinin altında metin yazmıyor; **ısı
haritası legend'ı her zaman çiziliyor** (Karar 7); `Bullet`
gerçekleşen değer aralığın dışındayken işareti kırpıyor; renk
`fill`/`stroke` özniteliğiyle veriliyor, `style` ile değil (Karar 6).

**React:** tick geldiğinde sparkline yeniden render edilmiyor
(emsal: `live/hooks.test.tsx:78-92`); `COMP` sembol listesi değişince
yeniden sorguluyor.

**tests/unit:** `/ui/api/sparklines` sınırları (201 sembol → 422,
`points=100` → 422, `interval` parametresi kabul edilmiyor); `missing`
içeriği; zarfın `Resource` olması. OpenAPI'de görünmeme ayrıca test
edilmez: router zaten `include_in_schema=False` (`ui/data.py:49`).

**tests/repo:** rota gerçek şemadan doğru kapanışları, doğru sırayı ve
`first_date`/`last_date`'i döndürüyor.

## Kapsam dışı

Çizim araçları ve teknik indikatörler; PNG/CSV dışa aktarma; `COV`
arşiv kapsama ısı haritası (`bar_gaps`); tablo şablonları (Koyfin'in
kolon setleri); `MKT` sparkline'ı; panel başına tema; replay;
parser'ın "iki mnemonik" belirsizliğinin düzeltilmesi.

## Kardeş spec ile kesişme

1. **Renk paleti.** Bu spec `styles.css`'te `--group-a…g`'yi tanımlar;
   2a onu grup kenar çizgisi ve rozeti için tüketir.
2. **Panel içinden gezinme.** `HEAT`'in Enter davranışı bugün `useGo`
   ile küresel gezinmedir; 2a bunu panel bağlamına taşıdığında `HEAT`
   ve `COMP` de o bağlamı kullanır.
3. **Abonelik bütçesi.** `COMP` (≤7 sembol) sayfa başına WS abonelik
   bütçesine dahildir; bütçe ve taşma davranışı 2a'da karara bağlanır.

## Uygulama sırası

| # | Alt proje | Çıktı | Bağımlılık |
| --- | --- | --- | --- |
| 2c-1 | Primitifler ve sparkline | `viz/` + CSS renk değişkenleri, `extra` kolon yolu, `/ui/api/sparklines`, `WLA`/`EQS`/`SCR` kolonu | yok |
| 2c-2 | Yeni görünümler | `HEAT`, `COMP` | 2c-1 |
| 2c-3 | Mevcut paneller | `FA`, `ANR`, `HDS`, `CA` grafikleri | 2c-1 |

2c-1 ile faz 2a-1 **paralel yürütülebilir**; aralarındaki tek bağ
2a-2'nin 2c-1'de tanımlanan grup paletini beklemesidir.

## Revizyonlar

2026-09-08, üç bağımsız inceleme sonrası:

- **Sorgu düzeltildi.** İlk taslak `symbol = ANY(:symbols) AND ts >=
  :since` yazıyordu; öyle bir tablo yok. Günlük kapanışlar
  `price_history`'de ve anahtar `session_date`.
- **Kolon tipi motorun kendi tipi oldu.** `extraColumns { key, header,
  render }` uydurmaydı; motor `Column<Row> { key, label, align?,
  format? }` kullanıyor. Ayrıca `WLA` ve `EQS`'in `DatasetTable`'dan
  geçmediği görüldü: sparkline üç panelde üç yoldan gelir.
- **Yanıt zarfa alındı.** `/ui/api`'nin her rotası `Collection`/
  `Resource` döndürüyor; çıplak gövde kalıba aykırıydı.
- **Renk tek kaynağa indi.** Yön renkleri bugün CSS'te çıplak hex;
  `colors.ts` onları TS'te yeniden tanımlasaydı iki kaynak olurdu.
  `Chart.tsx`'in `theme()` kalıbı zaten bu sorunu bir kez çözmüş.
- **`CATEGORICAL` paleti kaldırıldı**, `COMP` grup paletini kullanır ve
  yedi sembolle sınırlanır: "sekizinci renk ayırt edilemiyor" gerekçesi
  iki spec'te iki farklı sayı vermemeli.
- **`role="img"` ikiye ayrıldı:** tıklanabilir `HEAT` kutuları
  `role="img"` altında erişilebilirlik ağacından düşerdi.
- **`MKT` partiden çıkarıldı:** `market_summary_history` günlük kapanış
  serisi değil.
- **`COMP` `single` oldu ve dönemleri `6m/1y/3y`'ye indi** (sayfa
  tavanı 1000 satır).
- **Sıralama gerekçesi düzeltildi:** "2c önce, çünkü `Shell.tsx` başka
  dalda açık" iddiası geçersizdi — `worktree-web-terminal-1b` bu spec
  yazılmadan önce main'e merge edilmişti (`85d6c7b`). İki alt proje
  paralel yürür.
- Dönem argümanları enum oldu; erişilebilirlik örneği İngilizce'ye
  çevrildi (UI dizeleri İngilizce); `Dosyalar`, `Ölçümler` ve bu bölüm
  eklendi.
