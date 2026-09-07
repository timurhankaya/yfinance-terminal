# Web terminal faz 2a: yerleşim, gruplar ve kayıtlı sayfalar

Status: approved, not yet implemented
Date: 2026-09-08
`2026-09-07-web-terminal-design.md`'nin çocuğu;
`2026-09-08-web-terminal-faz2c-viz-design.md` ile kardeş. 2c önce
uygulanır (gerekçe: "Uygulama sırası"). Tek kesişme grup renkleridir:
palet 2c'nin `viz/colors.ts` dosyasında tanımlıdır, burada tüketilir.

## Neden

Terminal bugün tek panelli: bir adres, bir `(sembol, fonksiyon, arg)`
üçlüsü, bir görünüm. Bir profesyonelin ekranı böyle değil — grafik,
tape, haber ve mali tablo aynı anda durur ve bir sembol yazınca hepsi
birlikte döner. Faz 1 bunu bilerek engellemedi: paneller sembolü prop
olarak alır, `PanelSpec` yerleşim bilgisi taşımaz, WS aboneliği
bağlantı başına ve referans sayımlıdır. Bu spec o üç garantiyi kullanır.

### Mevcut durum, 2026-09-08'de doğrulandı

| Gerçek | Yer |
| --- | --- |
| `dockview` 8.2.0, MIT, tek bağımlılığı `dockview-core`; React'te `DockviewReact` + `components` haritası, `onReady` → `api`, `api.toJSON()/fromJSON()`, `api.onDidLayoutChange` | npm, dockview docs |
| v6'dan beri `dockview.css` **otomatik enjekte edilmiyor**, elle import ediliyor → Vite ayrı bir CSS dosyasına derler, mevcut `style-src 'self'` yeter | dockview "what's new v6" |
| Popout (paneli ayrı pencereye çıkarma) yeni pencereye `<style>` enjekte eder ve `style-src`'te **nonce** ister | dockview security docs |
| Sayfa CSP'si `default-src 'self'; …; style-src 'self'`, nonce yok; `index.html` statik ve `no-store` | `ui/pages.py:29-38` |
| `Shell` bugün `useParams` ile `(symbol, code)` okuyor, `pathToCommand`/`commandToPath` ile URL↔komut çeviriyor, `localStorage["yfin.ui.last"]` yazıyor, klavyenin tek sahibi | `web/src/app/Shell.tsx`, `app/keys.ts` |
| `Shell` şu anda açık işin içinde: `/ui/m/:code` market rotaları ve sembolü history entry'sinde taşıyan `HistoryContext` giriyor | `worktree-web-terminal-1b` |
| Store sembol başına abone sayıyor; sıfıra inince `unsub`; bağlantı başına en çok 200 sembol | faz 1c |
| 200 sembol bir karede 0,024 ms (bütçenin %0,14'ü); darboğaz React, store değil | `docs/measurements/websocket.md` |

## Kararlar

1. **Tek model: her sayfa bir yerleşimdir.** Ayrı bir "klasik mod"
   yoktur; bölünmemiş sayfa tek gruplu, tek sekmeli bir yerleşimdir ve
   ekranda bugünküyle aynı görünür. Reddedilen alternatif: klasik kabuk
   + ayrı workspace modu. İki mod, `Esc`'in iki anlamı, sembol
   bağlamının iki kavramı (şerit / grup harfi) ve iki kalıcılık yolu
   demekti; reponun "çift yol yok" kuralı burada gerçek bir bedeli
   önlüyor. Bedeli `Shell`'in yeniden yazılmasıdır, bir kereliktir.
2. **`dockview` 8.2, popout kapalı.** Popout nonce ister; `index.html`
   sunucu tarafında üretilmiyor ve CSP `'unsafe-inline'` taşımıyor.
   Yüzen (floating) gruplar sayfa içinde kaldıkları ve konumu CSSOM ile
   yazdıkları için CSP sorunu değildir, ama 2a'da kapalıdır: kayıtlı
   sayfa şemasını gereksiz büyütür.
3. **Grup harfi panelin özelliğidir, dockview grubunun değil.**
   Bloomberg'in modeli: harf ekranda nerede durduğundan bağımsızdır,
   paneli sürüklemek bağlamını değiştirmez. Reddedilen alternatif
   (sekme yığını = grup) paneli taşımayı sessiz bir bağlam değişimine
   çevirirdi.
4. **Harf + renk.** Harf kimlik, renk sinyaldir (Koyfin'in yedi renk
   grubunun karşılığı); panelin sol kenarında ince bir şerit ve sekmede
   bir rozet. Palet 2c'nin `GROUP` paletidir — yön (yeşil/kırmızı) ve
   vurgu (amber) renklerinden ayrıdır.
5. **Komut odaklı paneli yerinde değiştirir.** Bugünkü davranışın
   aynısı; bölünmemiş sayfada fark yoktur. Yeni panel açmak ayrı bir
   jesttir (`Ctrl/Meta+Enter`). Reddedilen alternatif "her komut yeni
   sekme", tek panelli kullanıcının bugünkü davranışını bozardı.
6. **`Esc` yalnız bakılanı geri alır.** History girdisi
   `(sayfa, grup sembolleri, odaklı panelin komutu)`'dur. Panel açmak,
   kapatmak, sürüklemek history'ye **girmez**: onlar düzenlemedir,
   gezinme değil. Böylece `Esc` her yerde tek anlam taşır.
7. **Sayfa adlandırılır, F-tuşu sıradan gelir.** `PG` panelindeki sıra
   `F1`–`F12`'ye karşılık gelir. Reddedilen alternatif (sabit 12 slot)
   adresi anlamsızlaştırır (`/ui/w/3`) ve sayfa sayısını on ikiyle
   sınırlar.
8. **Sürümlü zarf, göç yok.** Depodaki sayfa sürümü uymuyorsa ya da
   JSON bozuksa **o sayfa düşer**, varsayılana dönülür ve kullanıcıya
   yazılır. Göç kodu yazılmaz: yerleşim kaybı geri alınamaz bir veri
   kaybı değil, yeniden kurulabilir bir tercih.
9. **Paylaşım kodlanmış linkle.** `SHARE` o anki sayfayı
   `base64url(JSON)` olarak `/ui/w/-?l=…` linkine çevirir. 4000
   karakteri aşarsa komut reddeder ve nedenini yazar. Sunucuya hiçbir
   şey yazılmaz — terminal public ve kimliksizdir; sunucu tarafı
   kalıcılık faz 2b'nin konusudur.

## Mimari

```
web/src/app/
  Shell.tsx       komut kutusu, parser, klavye, palet, şerit, künye
  Workspace.tsx   dockview'i süren tek bileşen
  keys.ts         global kısayollar (F-tuşları eklenir)
web/src/workspace/
  page.ts         Page şeması, sürüm zarfı, encode/decode (paylaşım)
  store.ts        localStorage okuma/yazma, debounce, düşürme
  groups.ts       GroupLetter, grup → sembol çözümü
  PG.tsx          sayfa yöneticisi paneli
```

**Panel tarafı hiç değişmez.** dockview panel'i `params` olarak
`{ code, args, symbol, letter }` taşır; `components` haritasında tek
bir jenerik renderer vardır ve o `getPanel(code).component`'i çizer.
`PanelSpec`, `parseArgs`, `normalizeArgs`, `usePanelData`, tüm panel
testleri olduğu gibi kalır.

**Sembol çözümü tek kuraldır:** panelin harfi varsa sayfanın o harfe
yazdığı sembol, yoksa panelin kendi sembolü. Panel yine `symbol`
prop'unu alır ve kendisinin bir grupta olduğunu bilmez.

```ts
enum GroupLetter { A = "A", B = "B", C = "C", D = "D",
                   E = "E", F = "F", G = "G" }

interface PanelState {
  code: string;
  args: PanelArgs;
  symbol: string | null;          // harfsiz panelin kendi sembolü
  letter: GroupLetter | null;
}

interface Page {
  v: 1;
  name: string;                    // "-" = adsız çalışma sayfası
  groups: Partial<Record<GroupLetter, string>>;
  panels: Record<string, PanelState>;   // dockview panel id -> durum
  dock: SerializedDockview;             // api.toJSON()
}
```

Yedi harf, çünkü grup paleti yedi renklidir ve sekizinci renk ayırt
edilemiyor.

## Dilbilgisi eklemeleri

Faz 1'in üç dallı çözümlemesi korunur; üstüne iki kural gelir:

- **Tek token sembol** artık odaklı panelin **harfini** yeni sembole
  çevirir (harfsizse yalnız o paneli). `AAPL` yazmak A grubundaki dört
  paneli birden döndürür — grup harfinin bütün amacı budur.
- **`GRP <harf>|-`** odaklı paneli bir gruba bağlar ya da bağı koparır.
  `needsSymbol=false`, panel açmaz.
- **`PG SAVE <ad>`** çalışma sayfasını adlandırır; **`SHARE`** kodlanmış
  linki komut satırının altına yazar.

## Adres ve yönlendirme

| Adres | Anlam |
| --- | --- |
| `/ui/t/{SYMBOL}/{CODE}?args` | Tek gruplu sayfanın kısa yazımı — **bugünkü her link çalışmaya devam eder** |
| `/ui/m/{CODE}?args` | Aynısının sembolsüz (market) hâli |
| `/ui/w/{ad}` | Adlandırılmış kayıtlı sayfa |
| `/ui/w/-` | Adsız çalışma sayfası ("scratch") |
| `/ui/w/-?l=…` | Paylaşılan yerleşim; scratch'e yüklenir |

Kısa yazımdaki bir sayfa bölünür bölünmez `/ui/w/-` olur ve depoya
yazılır; yenilemede kaybolmaz. `PG SAVE trading` onu `/ui/w/trading`
yapar.

`/ui` kökü `yfin.ui.last`'a yönlendirir (bugünkü davranış); o da artık
bir sayfa adresi olabilir.

## Klavye

| Tuş | İş |
| --- | --- |
| `F1`–`F12` | `PG` sırasındaki sayfa |
| `Enter` | odaklı paneli yerinde değiştirir |
| `Ctrl/Meta+Enter` | sonucu yeni panel olarak sağa böler |
| `Alt+←/→/↑/↓` | odağı komşu panele taşır |
| `Esc` / `Shift+Esc` | history'de geri / ileri — değişmedi |
| `Ctrl/Meta+K`, `/`, `?` | değişmedi |

Panel kapatma sekmedeki × iledir; kısayol eklenmez (`Ctrl+W`
tarayıcınındır ve `Ctrl+Shift+W` pencereyi kapatır). Kısayollar bir
input odaktayken pasiftir (faz 1 kuralı); `F1`–`F12` bunun istisnasıdır
çünkü tarayıcı onları metin girişine yazmaz.

## Kalıcılık

- `localStorage["yfin.ui.pages"]`: `{ v: 1, order: string[], pages: Record<string, Page> }`.
  `order` F-tuşu sırasıdır.
- Yazma `api.onDidLayoutChange` üstünde 250 ms debounce ile; her tick
  değil, her yerleşim değişikliğinde.
- Okuma: `v` uymuyorsa ya da `JSON.parse` atarsa depo tamamen sıfırlanır
  ve kullanıcıya komut satırının altında bir kez yazılır. Kısmi kurtarma
  yok — yarısı okunmuş bir depo, kaybı gizlemenin uzun yoludur.
- `yfin.ui.last` bugünkü işini sürdürür.
- Depo yazılamıyorsa (özel mod) terminal çalışır, yalnız sayfalar
  kalıcı olmaz; şeritte bir kez uyarı.

## Canlı yol

Değişiklik yok. Store zaten sembol başına referans sayıyor: aynı
sembole bakan dört panel tek abonelik, sonuncusu kapanınca `unsub`.
Bir sayfa en çok yedi grup sembolü artı harfsiz panellerin kendi
sembollerini tutar; 200'lük bağlantı sınırının çok altında. `WLA`
zaten 200 sembole kadar ölçüldü.

## Hata yönetimi

- Bilinmeyen sayfa adı → uyarı ve `PG` paneli.
- Bozuk `?l=` → uyarı, boş scratch sayfası.
- `SHARE` 4000 karakteri aşıyor → komut reddedilir, kaç panel olduğu
  ve sınırın nedeni yazılır.
- dockview `fromJSON` atarsa → o sayfa düşer (Karar 8), varsayılan
  tek gruplu sayfa açılır.
- Depoda olmayan bir `code` (kaldırılmış mnemonik) → o panel
  "bilinmeyen fonksiyon" kartıyla açılır, sayfa düşmez.

## Testler

**vitest, saf:** `Page` yuvarlak gidişi (encode → decode özdeş);
sürüm uyumsuzluğunda düşürme; grup çözümü (harfli panel grubun
sembolünü, harfsiz kendi sembolünü alır); `GRP -` bağı koparınca panel
son sembolü tutar; `SHARE` uzunluk sınırı; F-tuşu ↔ `order` bağı.

**vitest, bileşen:** komut odaklı paneli değiştiriyor, komşusunu
değiştirmiyor; tek token sembol grubun tamamını döndürüyor;
`Ctrl+Enter` yeni panel açıyor; history girdisi yalnız bakılanı
taşıyor (panel açmak `history.length`'i artırmıyor); `Alt+←` odağı
taşıyor; bölünmemiş sayfa bugünkü DOM'u çiziyor.

**Değişmeyen:** tüm panel testleri (`PanelSpec` sözleşmesi aynı).
**Yeniden yazılan:** `Shell.test.tsx`.

**E2E (CI'da koşmaz):** `AAPL GIP` → `Ctrl+Enter` → `MSFT DES` →
`PG SAVE trading` → yenile → sayfa geri geliyor.

## Kapsam dışı

Faz 2b: layout'un DB'de tutulması, hosted kullanıcı tablosu, oturum.
Ayrıca: arşiv oynatma (replay), popout, panel başına tema, yüzen
gruplar, `PG`'de sayfa paylaşımının sunucu tarafı.

Bunların engellenmediğinin garantisi: `Page` taşınabilir bir JSON'dur
(DB'ye aynen yazılabilir), grup harfi panelin özelliğidir (replay'de
zaman ekseni sayfaya asılır, gruba değil) ve kalıcılık tek bir modülün
(`workspace/store.ts`) arkasındadır.

## Uygulama sırası

| # | Alt proje | Çıktı | Bağımlılık |
| --- | --- | --- | --- |
| 2a-1 | Yerleşim iskeleti | `Workspace`, dockview kurulumu ve teması, jenerik renderer, tek gruplu sayfanın bugünkü DOM'u | `Shell.tsx` main'de |
| 2a-2 | Gruplar | `GroupLetter`, renkler, `GRP`, tek token sembol kuralı, şerit | 2a-1, 2c-1 (palet) |
| 2a-3 | Sayfalar | `Page` şeması, depo, `PG` paneli, F-tuşları, `SHARE` | 2a-1 |

**Ön koşul.** 2a-1 `Shell.tsx`'i yeniden yazıyor ve o dosya 2026-09-08
itibarıyla `worktree-web-terminal-1b` dalında açık işin içinde. Bu
spec'in uygulaması o iş main'e indikten sonra başlar; faz 2c bu arada
bağımsız ilerler.
