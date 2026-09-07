# Web terminal faz 2c: görsel katman

Status: approved, not yet implemented
Date: 2026-09-08
`2026-09-07-web-terminal-design.md`'nin çocuğu;
`2026-09-08-web-terminal-faz2a-layout-design.md` ile kardeş ve ondan
**bağımsız**. İkisinin kesişmesi tek noktada: grup renkleri (aşağıda
"Renk rolleri"). Uygulama sırası 2c → 2a, gerekçesi "Uygulama sırası".

## Neden

Faz 1e "arşivdeki her şey terminalden okunur" sözünü tuttu: 61
dataset'in hepsi bir panelden görülebiliyor. Ama biri hariç hepsi
**tablo**. `GP`/`GIP` mum çiziyor, geri kalan her şey — sektör
metrikleri, screen rosterleri, gelir tabloları, hedef fiyatlar,
sahiplik dağılımları — sayı ızgarası olarak duruyor.

Bu bir kapsam eksiği değil, sunum eksiği: veri zaten diskte. Bu spec
hiçbir yeni fetch, hiçbir yeni tablo, hiçbir yeni Yahoo çağrısı
getirmez. Yalnızca yazılı olanı çizer.

### Rakip analizi, 2026-09-08

| Ürün | Bağlama | İmza görselleri | Alınan ders |
| --- | --- | --- | --- |
| Bloomberg Terminal / Launchpad | grup harfleri | `COMP`, sektör ısı haritası, `MOST`, `WEI` | `COMP` ve ısı haritası bizde yok; ikisi de mevcut veriden çizilebiliyor |
| Koyfin | 7 renk grubu | ısı haritaları, renk kodlu performans matrisleri, kaydedilmiş tablo şablonları | Gruba renk de ver: harf kimlik, renk sinyal. Tablo şablonu fikri `screen_quotes`'un 107 kolonu için not edildi (kapsam dışı) |
| OpenBB Workspace | parametre adına göre | widget başına tablo/grafik katmanı | Bağlamayı parametre adından türetmek bizde gereksiz: `PanelSpec` sembolü zaten prop alıyor |
| TradingView | layout sync | bar replay, screener ısı haritası | Replay ayrı bir faz 2 maddesi; ısı haritası buraya |
| Finviz | yok | treemap ısı haritasının kendisi ürün | Tek görünüm bir ürünü taşıyabiliyor; bizde `domains` ve `screen_quotes` ile mümkün |

Kaynaklar: docs.openbb.co/workspace, koyfin.com/features/custom-dashboards.

Ayırt edici nokta: bu ürünlerin hiçbirinde **kendi arşivinin sağlığı**
bir görünüm değil. Bizde `bar_gaps` var; `COV` (kapsama ısı haritası)
bu spec'in kapsamı dışında ama not edilmiştir.

### Mevcut durum, 2026-09-08'de doğrulandı

| Gerçek | Yer |
| --- | --- |
| `lightweight-charts` 5.2.1 kurulu; `GP`/`GIP` onu kullanıyor | `web/package.json`, `panels/chart-data.ts`, `panels/chart-live.ts` |
| `panels/table.tsx` hücre biçimini katalogun kolon türlerinden türetiyor; grid'de gizlenen tek kolon `raw_json` | `panels/table.tsx`, faz 1e |
| `/ui/api/screens/{key}` üye başına `price`, `change`, `change_percent`, `volume`, `market_cap`, `fifty_two_week_change_percent` döndürüyor | `ui/data.py:322-326,463-470` |
| `domain_metrics` `market_cap`, `market_weight`, `reg_market_change_pct`, `ytd_change_pct`, `one_year/three_year/five_year_change_pct` taşıyor | `models/domains.py:182-198` |
| Sayfa CSP'si `style-src 'self'`; inline `<style>` ve `style=` yok, konvansiyon `web/src`'de `style=` yasağı | `ui/pages.py:29`, faz 1 kararı |
| Sayılar sabit `en-US`, tarih-saatler UTC | faz 1e revizyonu |
| `/v1` sözleşmesi `openapi.json` ile byte-byte kilitli; UI'a özel okumalar `/ui/api/*` altında | `.github/workflows/ci.yml`, `ui/data.py` |
| Tarayıcı store'u 200 sembolde karede 0,024 ms; darboğaz React, store değil | `docs/measurements/websocket.md`, 2026-09-08 |

## Kararlar

1. **Yeni grafik bağımlılığı yok.** `web/src/panels/viz/` altında beş
   SVG primitifi yazılır. Reddedilenler ve nedenleri: **ECharts** kendi
   tipografisini, temasını ve tooltip'ini getirir, terminalin tek tip
   görünümünü bozar ve ~1 MB'tır; **Recharts** her hücre için bir React
   ağacı kurar, 200 satırlık bir tabloda 200 grafik bileşeni demektir;
   **visx** d3 modüllerini sürükler; **`lightweight-charts`'ı sparkline
   olarak kullanmak** hücre başına bir chart instance'ı (ve bir canvas)
   açar — yanlış araç. Primitifler saf SVG üretir, bu yüzden CSP,
   sunucu tarafı render ve test edilebilirlik sorunu doğurmazlar.
2. **`lightweight-charts` zaman serisinde kalır.** `GP`/`GIP` ona
   dokunulmaz; viz primitifleri onun karşılamadığı türler içindir
   (treemap, kategorik bar, bullet, scatter, hücre içi sparkline).
   İki kütüphane değil, iki iş.
3. **Grafik tabloyu değiştirmez, üstüne gelir.** `FA`, `ANR`, `HDS`,
   `CA` grafiklerini alır ama sayısal tabloları kalır. Gerekçe: faz
   1e'nin sözü "arşivdeki her şey okunur"du; bir grafiğin okunamayan
   tek şeyi kesin değeridir.
4. **Renk üç role ayrılır ve roller karışmaz.** Yön: yeşil/kırmızı.
   Vurgu ve odak: amber (`--accent`). Grup kimliği: ayrı, düşük
   doygunluklu yedi renklik palet. Bir renk iki anlam taşımaz — grup
   rengi yükselişi, yeşil odağı asla göstermez.
5. **Isı haritası ölçeği ıraksak (diverging) ve sıfırda nötrdür**, ve
   her zaman bir ölçek açıklaması (legend) ile gelir. Renk tek başına
   değer taşımaz: aynı sayı hücrenin içinde de yazılıdır (yer varsa) ve
   satır detayında her zaman.
6. **Toplu sparkline için tek yeni UI rotası.** 200 sembollük bir
   `WLA` için 200 REST çağrısı kabul edilemez; `/ui/api/sparklines`
   tek çağrıda son N günün kapanışlarını verir. `/v1` sözleşmesi
   değişmez (bu, UI'a özel beşinci okumadır; gerekçesi `news` ve
   `screens` ile aynı: generic yüzey bu şekli sunmuyor).
7. **Her grafik `role="img"` ve özet `aria-label` taşır.** Özet
   cümledir, veri dökümü değil ("11 sektör, en büyüğü Technology
   %1,32 yükselişte"). Ekran okuyucu için asıl kaynak yanındaki
   tablodur.
8. **SVG varsayılan; treemap'te hücre sınırı 400.** Fazlası tek bir
   "diğer" kutusunda toplanır ve panel bunu yazar. Canvas'a geçiş
   ölçüm görmeden yapılmaz — `store.bench.ts` kalıbıyla ölçülür.

## Mimari

```
web/src/panels/viz/
  scale.ts        linearScale, extent, niceTicks, normalize100
  colors.ts       DIRECTION (up/down/flat), CATEGORICAL (8), GROUP (7),
                  divergingHeat(pct) -> renk
  Sparkline.tsx   tek seri, eksensiz, opsiyonel alan dolgusu
  Bars.tsx        kategorik bar/grup bar + opsiyonel ikinci eksen çizgisi
  Bullet.tsx      aralık (low..high) + hedef işareti + gerçekleşen
  Treemap.tsx     squarified yerleşim, ıraksak renk, etiket eşiği
  Scatter.tsx     iki eksen + opsiyonel referans çizgisi
  index.ts
```

Her primitif saf bir bileşendir: veri prop'u alır, SVG döndürür, veri
çekmez, store'a bakmaz, `PanelSpec` bilmez. Yerleşim hesapları
(`squarify`, `linearScale`) ayrı saf fonksiyonlardır ve doğrudan test
edilir.

**Ortak sözleşme.** Her primitif `width`/`height` yerine
`viewBox` + `preserveAspectRatio` kullanır ve kapsayıcısını doldurur;
boyut CSS'in işidir (konvansiyon: `web/src`'de `style=` yok).

**Tablo grafik hücresi.** `panels/table.tsx`'e katalog kolon türünden
türetilen bir tip **eklenmez** — katalog `sparkline` diye bir tür
bilmiyor ve bilmemeli. Bunun yerine paneller kendi ek kolonlarını
bildirir: `extraColumns: { key, header, render(row) }`. `WLA`, `EQS`,
`SCR` bu yolla sparkline kolonu ekler; `DS` katalog gezgini olduğu için
eklemez.

## Yeni mnemonikler

| Kod | Şablon | Panel | Kaynak |
| --- | --- | --- | --- |
| `HEAT` | single | Isı haritası. `HEAT` sektörler; `HEAT <screen_key>` bir screen rosteri; dönem argümanı `1d` (varsayılan), `ytd`, `1y`, `3y`, `5y`. Alan = piyasa değeri, renk = seçilen dönemin değişimi. Enter bir kutuda `DOM` (sektör) ya da `DES` (sembol) açar. `needsSymbol=false` | `/ui/api/v1/datasets/domain_metrics`, `/ui/api/v1/datasets/domains`, `/ui/api/screens/{key}` |
| `COMP` | headed | Çoklu sembol karşılaştırma. `COMP AAPL MSFT NVDA [1y]`; başlangıç 100'e normalize günlük kapanışlar; en çok 8 sembol; dönem `6m`, `1y` (varsayılan), `3y`, `5y`, `max`. Şerit bağlam sembolünü gösterir, grafik listeyi | `/ui/api/v1/symbols/{s}/bars?interval=1d` |

`HEAT`'in dönem argümanı bedavaya geliyor: `domain_metrics` beş dönemi
de sütun olarak taşıyor. Screen varyantında yalnız `1d` ve `52w`
vardır (rota bu ikisini döndürüyor); panel diğer dönemleri sunmaz ve
nedenini yazar.

`COMP`'un sembol listesi URL'dedir (`?symbols=AAPL,MSFT,NVDA`), yani
`WLA` gibi paylaşılabilir.

## Zenginleşen paneller

| Panel | Eklenen görsel | Kaynak |
| --- | --- | --- |
| `FA` | Gelir ve net kâr grup barları + net marj çizgisi (ikinci eksen); yıllık/çeyrek sekmesini izler | `financials` |
| `ANR` | Hedef fiyat bullet'ı (low/mean/high aralığı, işaret son fiyat) + tavsiye dağılımının aya göre yığılmış barı | `analyst_price_targets`, `recommendations` |
| `HDS` | Top-N kurumsal sahiplik barı + insider net akışı (alım − satım) zaman serisi | `institutional_holders`, `insider_transactions` |
| `CA` | Temettü geçmişi barı; split'ler işaret olarak | `/ui/api/v1/symbols/{s}/actions` |
| `WLA`, `EQS`, `SCR` | 30 günlük sparkline kolonu | `/ui/api/sparklines` |
| `MKT` | Endeks kartları + sparkline | `market_summary_history` |

Hiçbiri yeni rota istemez; tek istisna sparkline kolonudur.

## `/ui/api/sparklines`

```
GET /ui/api/sparklines?symbols=AAPL,MSFT&days=30
->  { "days": 30,
      "series": [ { "symbol": "AAPL",
                    "closes": ["232.51", "231.90", ...],
                    "first_date": "2026-08-09",
                    "last_date": "2026-09-08" } ],
      "missing": ["ZZZZ"] }
```

- Yalnız `interval=1d` kapanışları; başka aralık yok (sparkline günlük
  bir şekildir ve intraday'i eklemek `PX`'in işini ikinci kez yapardı).
- `symbols` en çok 200 (WS bağlantı sınırıyla aynı sayı, aynı gerekçe);
  `days` 5–90, varsayılan 30. Aşımda 422.
- Arşivde barı olmayan sembol `missing`'e girer; sessizce düşmez —
  panel "veri yok" hücresi çizer.
- Ondalıklar `paging.to_number` ile (terminalin geri kalanıyla aynı
  dize), `normalize()` değil.
- Fren: `/ui/api` altında olduğu için mevcut `RequestBrake` kapsar.
- Tek sorgu: `(symbol, ts) IN` yerine `symbol = ANY(:symbols) AND
  ts >= :since` ve süreç içinde grupla; 200 sembol × 30 gün = 6.000
  satır.

## Performans

Bir `WLA` sayfası 200 sparkline × 30 nokta = 6.000 SVG path noktası
demektir. Bu, karede yeniden çizilmez: sparkline'lar günlük veriden
gelir ve tick akışıyla güncellenmez — canlı olan yalnız fiyat ve
değişim hücreleridir. Bu ayrım koda yazılır (sparkline `React.memo`
ile sembol+gün anahtarına bağlıdır) ve bir testle kilitlenir: bir tick
geldiğinde sparkline yeniden render **edilmez**.

Treemap 400 hücreye kadar SVG'dir; ölçüm `viz/treemap.bench.ts` ile
`store.bench.ts` kalıbında yapılır ve sonucu
`docs/measurements/`'a yazılır.

## Erişilebilirlik

- Her grafik `role="img"` + özet `aria-label`.
- Renk hiçbir yerde tek taşıyıcı değil: yön oku/işaret ya da yazılı
  değer eşlik eder. Yeşil/kırmızı çifti kırmızı-yeşil renk körlüğünde
  ayırt edilemez, bu yüzden yön ayrıca `+`/`−` ile yazılır.
- `prefers-reduced-motion`: grafiklerde geçiş animasyonu yok (zaten
  terminalin hiçbir yerinde yok).

## Hata yönetimi

- Dataset boş → "veri yok" kartı, grafik çizilmez.
- Kısmi veri (bir sembolün barı yok) → o seri çizilmez, adı
  "veri yok" olarak listelenir; diğerleri çizilir.
- `as_of` eski → başlıkta tarih yazılı; grafik gizlenmez.
- `HEAT` bilinmeyen screen anahtarı → uyarı ve `EQS` önerisi.
- `COMP` 8'den fazla sembol → uyarı, ilk 8 çizilmez; komut reddedilir.

## Testler

**vitest, saf fonksiyonlar:** `linearScale` uçları, `niceTicks`
aralıkları, `normalize100` (ilk nokta = 100, sıfır bölme yok),
`squarify` alan koruması (hücre alanları toplamı ≈ kutu alanı,
her hücre kutunun içinde), `divergingHeat` sıfırda nötr ve uçlarda
doygun.

**vitest, bileşen:** her primitifin `aria-label`'ı; `Sparkline` tek
noktalı seride çökmüyor; `Treemap` etiket eşiğinin altında metin
yazmıyor; `Bullet` gerçekleşen değer aralığın dışındayken işareti
kırpıyor.

**React:** tick geldiğinde sparkline yeniden render edilmiyor (bkz.
Performans); `COMP` sembol listesi değişince yeniden sorguluyor.

**Python unit:** `/ui/api/sparklines` sınırları (201 sembol → 422,
`days=100` → 422), `missing` içeriği, `interval` parametresi yok
(kabul edilmiyor), OpenAPI belgesinde görünmüyor.

**tests/repo:** rota gerçek şemadan doğru kapanışları ve tarih
aralığını döndürüyor.

## Kapsam dışı

Çizim araçları ve teknik indikatörler (`GIP` üstünde); PNG/CSV dışa
aktarma; `COV` arşiv kapsama ısı haritası (`bar_gaps`; kendi turunu
hak ediyor); tablo şablonları (Koyfin'in kolon setleri — `DS` ve
`screen_quotes` için not edildi); panel başına tema; replay.

## Uygulama sırası

| # | Alt proje | Çıktı | Bağımlılık |
| --- | --- | --- | --- |
| 2c-1 | Primitifler ve sparkline | `viz/` beş primitif + `scale`/`colors`, `extraColumns`, `/ui/api/sparklines`, `WLA`/`EQS`/`SCR` kolonu | yok |
| 2c-2 | Yeni görünümler | `HEAT`, `COMP` | 2c-1 |
| 2c-3 | Mevcut paneller | `FA`, `ANR`, `HDS`, `CA`, `MKT` grafikleri | 2c-1 |

**2c neden 2a'dan önce.** Bu spec `Shell.tsx`'e hiç dokunmuyor: yeni
paneller, panellerin kendi bildirdiği kolonlar ve yeni bir modül. Faz
2a ise `Shell.tsx`'in URL↔panel eşlemesini yeniden yazıyor ve o dosya
2026-09-08 itibarıyla `worktree-web-terminal-1b` dalında açık işin
içinde (`/ui/m/:code` market rotaları, `HistoryContext`). 2c beklemeden
başlar; 2a o iş main'e indiğinde başlar.

**2a ile tek kesişme:** grup renkleri. `viz/colors.ts` yedi renklik
`GROUP` paletini bu spec'te tanımlar; 2a onu tüketir. Ters yönde
bağımlılık yok.
