# Web terminal faz 2a: yerleşim, gruplar ve kayıtlı sayfalar

Status: approved, not yet implemented
Date: 2026-09-08
Revised 2026-09-08 after three independent reviews (see "Revizyonlar").
`2026-09-07-web-terminal-design.md`'nin çocuğu;
`2026-09-08-web-terminal-faz2c-viz-design.md` ile kardeş ve onunla
paralel yürür. Kesişmeler "Kardeş spec ile kesişme"de sayılıdır.

## Neden

Terminal bugün tek panelli: bir adres, bir `(sembol, fonksiyon, arg)`
üçlüsü, bir görünüm. Bir profesyonelin ekranı böyle değil — grafik,
tape, haber ve mali tablo aynı anda durur ve bir sembol yazınca hepsi
birlikte döner.

Faz 1 bunu bilerek engellemedi ve üç garanti verdi: paneller sembolü
prop olarak alır; `PanelSpec` yerleşim bilgisi taşımaz, yalnızca şablon
tercihi; WS abonelik seti bağlantı başına ve referans sayımlıdır. Bu
spec üçünü de kullanır — ve birincisinin bugün **tam olarak doğru
olmadığını** düzeltir (aşağıda Karar 4).

### Mevcut durum, 2026-09-08'de doğrulandı

| Gerçek | Yer |
| --- | --- |
| `dockview` 8.2.0, MIT, tek bağımlılığı `dockview-core@^8.2.0` | npm |
| React API: `DockviewReact` + `components` haritası, `onReady` → `event.api`, `api.addPanel({id, component, title, params, position})`, `api.toJSON()/fromJSON()`, `api.onDidLayoutChange` | dockview.dev/docs/core |
| `dockview.css` elle import edilir (otomatik enjekte edilmez) → Vite ayrı bir CSS dosyasına derler, `style-src 'self'` yeter | dockview.dev/docs/core/theming |
| CSP ile etkileşen **tek** dockview özelliği popout'tur: yeni pencerede inline `<style>` yeniden kurulur ve nonce ister | dockview.dev/docs/core/security |
| Sayfa CSP'si `default-src 'self'; …; style-src 'self'`, nonce yok; `index.html` statik ve `no-store` | `ui/pages.py:26-38` |
| Sunucu yalnız `/ui`, `/ui/`, `/ui/t/{path:path}`, `/ui/m/{path:path}` rotalarını kaydediyor; `/ui/{path:path}` catch-all'ı **bilinçli olarak** reddedilmiş | `ui/pages.py:58-67` |
| İstemci rotaları: `/ui` (HOME), `/ui/m/:code`, `/ui/t/:symbol/:code`, gerisi `HOME_PATH`'e `replace` | `web/src/app/App.tsx:18-21` |
| **`localStorage` terminalde yok.** `yfin.ui.last` kaldırıldı; `/ui` gerçek bir anasayfa | `App.tsx:13-17`, `App.test.tsx:166-173`, parent revizyonu |
| Bağlam sembolü history entry'sinde taşınıyor (`navigate(path, { state: { symbol } })`), tek yerden: `useGo` | `web/src/commands/go.ts:1-28` |
| **Yedi panel `useGo` çağırıyor**, yani panel içinden tüm uygulamanın adresini değiştiriyor | `EQS.tsx:80,159,216`, `WLA.tsx:107`, `FA.tsx:105`, `HOME.tsx:46`, `DS.tsx:22`, `curated.tsx:63`, `HELP.tsx:56` |
| **`useListKeys` window seviyesinde `keydown` dinliyor** ve panel odağı diye bir kavramı yok | `web/src/panels/common.tsx:237-261`; kullananlar `EQS`, `N`, `WLA`, `DS`, `CF`, `table.tsx` |
| `PanelSpec` alanları: `code, title, needsSymbol, usage?, layout, parseArgs, normalizeArgs?, component`; `PanelProps` = `{ symbol, args }` | `web/src/commands/types.ts:5-40` |
| `Shell` URL'den okunan args'ı `normalizeArgs` ile düzeltiyor ve komut gönderiminde sembolü `getSymbol` ile doğruluyor | `Shell.tsx:74-77,96-107` |
| Şerit `Shell`'de, tek ve küresel; `spec.layout === Layout.Headed` ise canlı | `Shell.tsx:213-219` |
| Store sembol başına referans sayıyor, sıfırda `unsub`; istemci `Op.Error`'ı bilinçli olarak yok sayıyor | `live/store.ts:120-125,152-167` |
| Bağlantı başına 200 sembol ve sınır **kümülatif**: aşımda `sub` çerçevesinin tamamı reddedilir | `ui/live.py:83,384` |
| `WLA` tek başına 200 sembole abone olabilir | `web/src/panels/WLA.tsx:30` |
| `worktree-web-terminal-1b` main'e merge edildi; `Shell.tsx`, `go.ts`, `pages.py` main'de | `git log`: `1dc1393`, `85d6c7b` |

## Kararlar

1. **Tek model: her sayfa bir yerleşimdir.** Ayrı bir "klasik mod"
   yoktur; bölünmemiş sayfa tek gruplu, tek sekmeli bir yerleşimdir ve
   ekranda bugünküyle aynı görünür. Reddedilen alternatif — klasik
   kabuk + ayrı workspace modu — `Esc`'in iki anlamı, sembol
   bağlamının iki kavramı ve iki kalıcılık yolu demekti.
2. **`dockview` 8.2, popout kapalı.** Popout nonce ister;
   `index.html` sunucu tarafında üretilmiyor ve CSP `'unsafe-inline'`
   taşımıyor. Yüzen (floating) gruplar da 2a'da kapalıdır: kayıtlı
   sayfa şemasını gereksiz büyütürler.
   **Konvansiyon notu:** parent `@tanstack/react-virtual`'ı "mutlak
   konumlandırma ister, o da terminalin tek inline stili olurdu"
   diye reddetmişti. O kural **bizim yazdığımız JSX** içindir; dockview
   konumu CSSOM'dan yazar (`element.style.*`), bu ne CSP'ye takılır ne
   de `web/src`'de bir `style=` özniteliği doğurur. Kural değişmiyor,
   kapsamı yazılıyor.
3. **Grup harfi panelin özelliğidir, dockview grubunun değil.** Harf
   ekranda nerede durduğundan bağımsızdır; paneli sürüklemek bağlamını
   değiştirmez. Reddedilen alternatif (sekme yığını = grup) taşımayı
   sessiz bir bağlam değişimine çevirirdi.
4. **Panelin *props* sözleşmesi değişmez; panelin *gezinme* ve
   *klavye* bağlamı değişir.** `PanelProps` (`{ symbol, args }`) ve
   `PanelSpec` aynı kalır, panel testleri aynı kalır. Ama iki mekanizma
   bugün küreseldir ve çok panelde çalışmaz:
   - **`useGo`** yedi panelde tüm uygulamanın adresini değiştiriyor.
     Çok panelli sayfada `EQS` satırında Enter, kayıtlı sayfayı terk
     edip `/ui/t/AAPL/DES`'e giderdi. `useGo` panel bağlamına taşınır:
     odaklı paneli **yerinde** değiştirir.
   - **`useListKeys`** window'da dinliyor. Dört liste paneli açıkken
     `j` dördünü birden hareket ettirir ve `Enter` dört gezinme
     tetikler. Panel köküne ve "odaklı panel" kontrolüne bağlanır.
   Bu iki taşıma 2a-1'in çıktısıdır ve işin büyük kısmıdır.
5. **Komut odaklı paneli yerinde değiştirir.** Bugünkü davranışın
   aynısı; bölünmemiş sayfada fark yoktur. Yeni panel açmak ayrı bir
   jesttir.
6. **`Esc` yalnız bakılanı geri alır.** History girdisi
   `(sayfa, grup sembolleri, odaklı panel, onun komutu)`'dur. Panel
   açmak, kapatmak, sürüklemek history'ye **girmez** — onlar
   düzenlemedir. `Ctrl/Meta+Enter` hem panel açar hem odağı taşır:
   **açma history'ye girmez, odak değişimi girer**; girdinin panel
   kimliği yeni panelin kimliğidir.
7. **Sayfa adlandırılır, F-tuşu sıradan gelir.** Reddedilen alternatif
   (sabit 12 slot) adresi anlamsızlaştırır ve sayfa sayısını sınırlar.
8. **Kalıcılık geri geliyor ve bu bilinçli bir geri adımdır.** Parent
   "`localStorage` yalnız yönlendirme içindir, ikinci bir durum kaynağı
   değildir" demiş, sonra onu tamamen kaldırmıştı. Bir yerleşim URL'e
   sığmaz (`WLA`'nın sembol listesi sığıyordu, dört panelin ızgarası
   sığmıyor), bu yüzden `yfin.ui.pages` terminalin URL dışındaki ilk
   kalıcı durumudur. `yfin.ui.last` **diriltilmez**: `/ui` bir
   anasayfadır.
9. **Bozulmada iki ayrı granülerlik.** Depo seviyesi hatası
   (`JSON.parse` atar, zarf `v` uymuyor) → depo sıfırlanır. Sayfa
   seviyesi hatası (`fromJSON` atar, bilinmeyen `code`) → yalnız o
   sayfa düşer. Göç kodu yazılmaz: yerleşim kaybı geri alınamaz bir
   veri kaybı değil, yeniden kurulabilir bir tercihtir.
10. **Paylaşım kodlanmış linkle**, sunucuya hiçbir şey yazılmadan:
    terminal public ve kimliksizdir; sunucu tarafı kalıcılık faz
    2b'nin konusudur.
11. **Harf yalnız tek sembollü panellere takılır.** `needsSymbol=false`
    olanlar (`HEAT`, `MKT`, `CAL`, `HELP`) ve sembol listesini argüman
    olarak taşıyanlar (`WLA`, `EQS`, `COMP`) gruba **bağlanamaz**:
    `COMP`'un gerçek durumu `?symbols=`'dedir, bir harf orada yalnız
    şeridi değiştirir ve grafiği değiştirmediği için görünür biçimde
    yalan söylerdi. `GRP` bu panellerde reddedilir ve nedenini yazar.
12. **Şerit panele iner.** Bugün `Shell` tek küresel şerit çiziyor.
    Çok panelde iki `headed` panelin iki farklı sembolü olabilir, tek
    şerit hangisini göstereceğini söyleyemez. `Layout.Headed` artık
    "bu panel kendi başlığında canlı fiyat şeridi taşır" demektir ve
    şerit dockview panelinin içine iner. `Layout` enum'u değişmez;
    anlamı panel çerçevesine bağlanır.

## Mimari

```
web/src/app/
  Shell.tsx       komut kutusu, parser, klavye, palet, künye
  Workspace.tsx   dockview'i süren tek bileşen
  keys.ts         global kısayollar (sayfa tuşları, odak taşıma)
web/src/workspace/
  page.ts         Page şeması, sürüm zarfı, encode/decode (paylaşım)
  store.ts        localStorage okuma/yazma, debounce, düşürme
  groups.ts       GroupLetter, harf → sembol çözümü, GRP kuralları
  panel.ts        PanelContext: odaklı panelde komut çalıştırma, odak
  PG.tsx          sayfa yöneticisi paneli
```

dockview panel'i `params` olarak `{ code, args, symbol, letter }`
taşır; `components` haritasında tek bir jenerik renderer vardır ve o
`getPanel(code).component`'i çizer. Sembol çözümü tek kuraldır:
panelin harfi varsa sayfanın o harfe yazdığı sembol, yoksa panelin
kendi sembolü. Panel yine `symbol` prop'unu alır ve bir grupta olduğunu
bilmez.

`normalizeArgs` ve `getSymbol` doğrulaması bugün yalnız URL okuma ve
komut gönderme yollarında çalışıyor (`Shell.tsx:74-77,96-107`).
Kayıtlı sayfadan gelen args ve semboller de aynı iki süzgeçten geçer:
`fromJSON` sonrası her panelin args'ı `normalizeArgs`'tan geçirilir;
sembol doğrulaması tembeldir (panel 404 alırsa kendi hata kartını
çizer), çünkü bir sayfayı açarken yedi ek istek atmak açılışı
sembol sayısı kadar yavaşlatırdı.

### Sayfa şeması

```ts
enum GroupLetter { A = "A", B = "B", C = "C", D = "D",
                   E = "E", F = "F", G = "G" }
enum PageName { Scratch = "-" }
enum GroupCommand { Detach = "-" }

interface Page {
  name: string;                                  // PageName.Scratch = adsız
  groups: Partial<Record<GroupLetter, string>>;
  dock: SerializedDockview;                      // api.toJSON()
}

interface PageStore { v: 1; order: string[]; pages: Record<string, Page> }
```

**Panel durumu ayrı bir alanda tutulmaz.** `api.toJSON()` panel
`params`'ını zaten serileştirir; ikinci bir `panels` haritası aynı
bilginin ikinci kopyası olurdu ve her sürükleme, kapatma ya da `GRP`
çağrısı ikisini ayrıştırırdı. Tek kaynak `dock` içindeki `params`;
sayfa seviyesinde yalnız `groups` durur. Sürüm alanı da tektir ve
zarftadır.

Panel id'si `code` + monoton sayaç (`gip-3`); `SHARE`'in karakter
bütçesi doğrudan id uzunluğuna bağlı olduğu için uuid kullanılmaz.

Sekme başlığı `SEMBOL KOD` (harfli panelde grup sembolüyle, harfsizde
kendi sembolüyle, sembolsüz panelde yalnız `KOD`); grup sembolü
değişince başlık güncellenir.

## Dilbilgisi eklemeleri

Parser'ın üç dallı çözümlemesi korunur ama **üçüncü dalın sonucu
değişir**: tek token sembol artık bir `Command` üretmez, bir grup
durumu yazar. `ParseResult`'a yeni bir `ParseKind.Symbol` eklenir
(`parser.ts:54-57,83-89`).

- **Tek token sembol** → odaklı panelin harfini yeni sembole çevirir
  (harfsizse yalnız o paneli). `AAPL` yazmak A grubundaki dört paneli
  birden döndürür; grup harfinin bütün amacı budur.
- **`GRP <harf>|-`** odaklı paneli bir gruba bağlar ya da bağı koparır
  (Karar 11'deki paneller hariç).
- **`PG SAVE <ad>`** çalışma sayfasını adlandırır; **`SHARE`**
  kodlanmış linki komut satırının altına yazar.

**Registry `PanelSpec`'ten fazlasını tutmak zorunda.** Bugün
`getPanel(code)` bulamazsa parser "Unknown function" hatası veriyor
(`parser.ts:60-61`) ve `listPanels()` fonksiyon çubuğunu, paleti ve
`HELP`'i besliyor (`Shell.tsx:163-165`). `GRP`/`SHARE`/`PG SAVE` sahte
panel olarak kaydedilseydi fonksiyon çubuğuna düşerlerdi. Bu yüzden
registry `CommandSpec` tutar:

```ts
enum CommandKind { Panel = "panel", Action = "action" }
```

`listPanels()` yalnız `Panel` döndürür; `HELP` ikisini iki başlık
altında listeler.

`GRP` ve `SHARE` mnemonik olarak ayrılır; aynı adı taşıyan bir ticker
`GRP DES` biçiminde erişilebilir kalır (parser'ın "iki mnemonik" kuralı
bunu zaten böyle çözüyor).

## Adres ve yönlendirme

| Adres | Anlam |
| --- | --- |
| `/ui` | HOME — değişmedi |
| `/ui/t/{SYMBOL}/{CODE}?args` | Tek gruplu sayfanın kısa yazımı |
| `/ui/m/{CODE}?args` | Aynısının sembolsüz (market) hâli |
| `/ui/w/{ad}` | Adlandırılmış kayıtlı sayfa |
| `/ui/w/-` | Adsız çalışma sayfası |
| `/ui/w/-?l=…` | Paylaşılan yerleşim |

**Sunucu tarafı zorunlu.** `pages.py` bugün `/ui/w/*`'yi bilmiyor;
`/ui/w/trading` yenilendiğinde SPA dönmez. `pages.py`'ye üçüncü client
rotası eklenir: `pages.add_api_route("/ui/w/{path:path}", spa, …)`.
Catch-all yine yazılmaz — gerekçesi parent'ta yazılı (`/ui/api/*`'yi
yutar). `App.tsx`'e `/ui/w/:name` rotası eklenir.

**İki adres şekli "çift yol" değildir.** Yayınlanmış bir URL bir
sözleşmedir, iç yol değil: `/ui/t/AAPL/DES` çalışmaya devam eder.
Yine de tek yönlüdür — o sayfa bölünür bölünmez `/ui/w/-`'ye taşınır
ve geri dönmez.

**Push mu replace mi.** Bölünme bir düzenlemedir (Karar 6), yani
`/ui/t/…` → `/ui/w/-` geçişi `replace`'tir: `Esc` bölünmemiş hâle
değil, ondan önceki adrese gider. Sayfa değiştirmek (`F2`, `PG`) ve
odaklı panelin komutunu değiştirmek `push`'tur.

**History state'i.** URL sayfayı adlandırır; grup sembolleri ve odak
`history.state`'te taşınır — `useGo`'nun bugün `{ symbol }` için
kullandığı yer (`go.ts:24`), aynı gerekçeyle: doğru ömür, history
girdisinin ömrüdür. Bu, paylaşılan bir `/ui/w/trading` linkinin grup
sembollerini **taşımadığı** anlamına gelir; o link sayfayı depodaki
son hâliyle açar, `?l=` ise yerleşimi de sembolleri de taşır.

**`?l=`'nin ömrü.** Çözüldükten sonra URL `/ui/w/-`'ye `replace`
edilir, yani bir sonraki gezinme onu yeniden uygulamaz. Çalışma sayfası
doluysa üzerine yazmadan önce komut satırının altında onay istenir
("Enter to replace the working page").

## Klavye

| Tuş | İş |
| --- | --- |
| `F1`–`F4`, `F7`–`F10` | `PG` sırasındaki sayfa (sekiz sayfa) |
| `Enter` | odaklı paneli yerinde değiştirir |
| `Ctrl/Meta+Enter` | sonucu yeni panel olarak sağa böler |
| `Ctrl+Shift+←/→/↑/↓` | odağı komşu panele taşır |
| `Esc` / `Shift+Esc` | history'de geri / ileri — değişmedi |
| `Ctrl/Meta+K`, `/`, `?` | değişmedi |

**Dışarıda bırakılan F-tuşları ve nedeni:** `F5` (yenile), `F11` (tam
ekran), `F12` (geliştirici araçları) tarayıcıda `preventDefault` ile
iptal **edilemez**; `F6` adres çubuğuna odaklanır. Sekiz sayfa yedi
grup harfi için fazlasıyla yeter.

**`Alt+ok` neden değil:** Windows ve Linux'ta tarayıcının geri/ileri
kısayolu; `Esc`/`Shift+Esc` zaten o işi yapıyor ve bir `preventDefault`
kaçağı odak taşırken sayfayı da geri alırdı. macOS'ta `Alt+ok` metin
alanında kelime atlar.

**Yönlü komşu bulma bizde.** dockview'in dokümanlarında yönlü komşu
gezinme API'si yok; odak taşıma, grup elemanlarının
`getBoundingClientRect()` kutularından hesaplanır (2a-1 kapsamında).

Kısayollar bir input odaktayken pasiftir (faz 1 kuralı); sayfa tuşları
bunun istisnasıdır.

## Kalıcılık

- `localStorage["yfin.ui.pages"]` = `PageStore`. `order` F-tuşu
  sırasıdır.
- Yazma `api.onDidLayoutChange` üstünde 250 ms debounce ile.
- Okuma: Karar 9'daki iki granülerlik.
- Depo yazılamıyorsa (özel mod) terminal çalışır, sayfalar kalıcı
  olmaz; bir kez uyarı.

## Canlı yol ve abonelik bütçesi

Store zaten sembol başına referans sayıyor: aynı sembole bakan dört
panel tek abonelik. **Ama bütçe sayfa başına düşünülmeli**: `WLA` tek
başına 200 sembole abone olabilir (`WLA.tsx:30`), `COMP` yediye kadar,
ve sunucu sınırı bağlantı başına **kümülatiftir** — aşımda `sub`
çerçevesinin tamamı reddedilir (`ui/live.py:384`). Bugün istemci
`Op.Error`'ı bilinçli olarak yok sayıyor (`store.ts:120-125`), yani
iki `WLA`'lı bir sayfada paneller sessizce ölü kalırdı.

Karar: store `too_many` hatasını **kaydeder**; şerit ve etkilenen
panel "abonelik bütçesi doldu (200 sembol)" yazar; kullanıcı bir
paneli kapatınca abonelik yeniden denenir. Bu 2a-3'ün çıktısıdır ve
bir testle kilitlenir.

## Hata yönetimi

- Bilinmeyen sayfa adı → uyarı ve `PG` paneli.
- Bozuk `?l=` → uyarı, çalışma sayfası olduğu gibi kalır.
- `SHARE` 4000 karakteri aşıyor → komut reddedilir. Sınırın nedeni:
  bir URL'in tarayıcıdan sunucuya ve oradan loglara kadar her durakta
  güvenli olduğu pratik eşik ~8 KB'tır (ters proxy başlık tamponu);
  base64 üçte bir şişirdiği için 4000 karakterlik yerleşim ~5,3 KB'lık
  bir adres eder ve payı bırakır.
- dockview `fromJSON` atarsa → o sayfa düşer, varsayılan tek gruplu
  sayfa açılır.
- Depoda olmayan bir `code` → o panel "bilinmeyen fonksiyon" kartıyla
  açılır, sayfa düşmez.
- Abonelik bütçesi aşımı → yukarıdaki bölüm.

## Görünüm ve dar ekran

dockview teması hazır sınıflardan biri taban alınmaz; `--dv-*`
değişkenleri `styles.css`'in kendi token'larına (`--bg`, `--fg`,
`--line`, `--muted`, `--accent`) eşlenir. Grup rengi panelin sol
kenarında ince bir **kenar çizgisi** ve sekmede bir rozettir; palet
2c'nin `--group-a…g` değişkenleridir. ("Şerit" sözcüğü bu spec'te
yalnız canlı fiyat şeridi için kullanılır.)

Dar ekranda (< 720 px) dockview tek gruba düşer ve sürükle-bırak
kapanır: sayfa yine açılır, panelleri sekme olarak gösterir. Kayıtlı
yerleşim bozulmaz, yalnız çizilmez.

## Ölçümler

`web/src/workspace/workspace.bench.ts` (`store.bench.ts` kalıbı):
1, 4, 8 panelli sayfada bir tick'in yol açtığı render sayısı ve
`fromJSON` süresi. Sonuç `docs/measurements/` altına yazılır. Ölçümün
sorusu şu: dört panel açıkken bir sembolün tick'i kaç bileşeni
yeniden çiziyor — `live/hooks.test.tsx:78-92` tek panelde bunu zaten
bire indirmişti.

## Dosyalar

**Yeni:** `web/src/app/Workspace.tsx`,
`web/src/workspace/{page,store,groups,panel}.ts`,
`web/src/workspace/PG.tsx`, ilgili `*.test.ts(x)`,
`web/src/workspace/workspace.bench.ts`.

**Değişen:** `web/src/app/Shell.tsx` (panel çizimi `Workspace`'e),
`web/src/app/keys.ts` (sayfa tuşları, odak taşıma),
`web/src/app/App.tsx` (`/ui/w/:name`), `web/src/app/styles.css`
(dockview değişken eşlemesi, grup kenar çizgisi),
`web/src/commands/{parser,registry,types}.ts` (`ParseKind.Symbol`,
`CommandSpec`, `CommandKind`), `web/src/commands/go.ts` (panel
bağlamı), `web/src/panels/common.tsx` (`useListKeys` odak kontrolü),
`useGo` çağıran yedi panel, `web/src/panels/HELP.tsx`,
`src/yfin/ui/pages.py` (`/ui/w/{path:path}`), `web/package.json`,
`web/src/app/main.tsx` (`dockview.css` import'u).

**Bağımlılıklar.** Runtime: `dockview` 8.2 (+ `dockview-core`), MIT,
tek npm eklemesi. Reddedilenler: **FlexLayout** (referans projede var
ama API'si eski tarz ve serileştirmesi dockview'inki kadar zengin
değil), **golden-layout** (React sarmalayıcısı üçüncü parti),
**react-mosaic** (sekme yığını yok), **react-grid-layout** (ızgara
var, dock yok), **kendi splitter'ımız** (sürükle-bırak, sekme yığını,
serileştirme ve erişilebilirlik sıfırdan).

## Testler

**vitest, saf:** `Page` yuvarlak gidişi (encode → decode özdeş); depo
seviyesi ve sayfa seviyesi bozulmanın farklı sonuçları (Karar 9);
harf → sembol çözümü; `GRP` Karar 11'deki panellerde reddediliyor;
`SHARE` uzunluk sınırı; F-tuşu ↔ `order` bağı; panel id sayacı.

**vitest, bileşen:** komut odaklı paneli değiştiriyor, komşusunu
değiştirmiyor; tek token sembol grubun tamamını döndürüyor;
`Ctrl+Enter` yeni panel açıyor ve odağı taşıyor; **iki liste paneli
açıkken `j` yalnız odaklı olanı hareket ettiriyor** (Karar 4);
**`EQS` satırında Enter sayfayı terk etmiyor** (Karar 4);
`Ctrl+Shift+←` odağı taşıyor; bölünmemiş sayfa bugünkü DOM'u çiziyor;
`headed` panel kendi şeridini taşıyor (Karar 12); abonelik bütçesi
aşımında uyarı görünüyor.

**tests/unit (Python):** `/ui/w/trading` ve `/ui/w/-` `index.html`
döndürüyor; `/ui/api/bilinmeyen` hâlâ 404 problem gövdesi veriyor
(catch-all eklenmediğinin kanıtı); `openapi.json` değişmemiş.

**Değişmeyen:** panel testleri (`PanelProps` sözleşmesi aynı).
**Yeniden yazılan:** `Shell.test.tsx`.

**E2E (CI'da koşmaz, `web/playwright.config.ts`):** `AAPL GIP` →
`Ctrl+Enter` → `MSFT DES` → `GRP B` → `PG SAVE trading` → yenile →
sayfa geri geliyor.

## Kapsam dışı

Faz 2b: layout'un DB'de tutulması, hosted kullanıcı tablosu, oturum.
Ayrıca: arşiv oynatma (replay), popout, yüzen gruplar, panel başına
tema, sunucu tarafı sayfa paylaşımı, parser'ın "iki mnemonik"
belirsizliğinin düzeltilmesi.

Bunların engellenmediğinin garantisi: `Page` taşınabilir bir JSON'dur
(DB'ye aynen yazılabilir), grup harfi panelin özelliğidir (replay'de
zaman ekseni sayfaya asılır, gruba değil) ve kalıcılık tek bir modülün
(`workspace/store.ts`) arkasındadır.

## Kardeş spec ile kesişme

1. **Renk paleti.** 2c `styles.css`'te `--group-a…g`'yi tanımlar; 2a
   onu kenar çizgisi ve rozet için tüketir.
2. **Panel içinden gezinme.** 2a `useGo`'yu panel bağlamına taşır;
   2c'nin `HEAT` Enter davranışı o bağlamı kullanır (2c tek panelli
   dünyada bugünkü `useGo` ile yazılır, 2a onu taşır).
3. **Abonelik bütçesi.** 2c'nin `COMP`'u (≤7 sembol) sayfa bütçesine
   dahildir; bütçe ve taşma davranışı burada karara bağlanır.

## Uygulama sırası

| # | Alt proje | Çıktı | Bağımlılık |
| --- | --- | --- | --- |
| 2a-1 | Yerleşim iskeleti | `Workspace`, dockview kurulumu ve tema eşlemesi, jenerik renderer, `pages.py` + `App.tsx` rotaları, `useGo` ve `useListKeys`'in panel bağlamına taşınması, tek gruplu sayfanın bugünkü DOM'u | yok |
| 2a-2 | Gruplar | `GroupLetter`, kenar çizgisi ve rozet, `GRP`, `ParseKind.Symbol`, `CommandKind`, şeridin panele inmesi | 2a-1, 2c-1 (palet) |
| 2a-3 | Sayfalar | `Page` şeması, depo, `PG` paneli, sayfa tuşları, `SHARE`, abonelik bütçesi uyarısı | 2a-1 |

2a-1 ile 2c-1 paralel yürütülebilir.

## Revizyonlar

2026-09-08, üç bağımsız inceleme sonrası:

- **`yfin.ui.last` iddiaları silindi.** Spec onları parent'ın eski
  bölümünden devralmıştı; `localStorage` terminalden tamamen
  kaldırılmıştı ve `/ui` gerçek bir anasayfa. Kalıcılığın geri gelişi
  artık bir karar olarak yazılı (Karar 8), sessiz bir devir değil.
- **"Panel tarafı hiç değişmez" daraltıldı.** Yedi panel `useGo` ile
  tüm uygulamanın adresini değiştiriyor ve `useListKeys` window'da
  dinliyor; ikisi de çok panelde bozulur. İşin büyük kısmı bu iki
  taşımadır ve artık 2a-1'in çıktısında yazılı.
- **`/ui/w/*` sunucu rotası eklendi.** Onsuz kendi E2E'si ("yenile →
  sayfa geri geliyor") koşmazdı.
- **`Page.panels` kaldırıldı.** dockview `params`'ı zaten
  serileştiriyor; ikinci kopya her sürüklemede ayrışırdı.
- **Bozulma iki granülerliğe ayrıldı** (depo / sayfa); iki `v` alanı
  bire indi.
- **Klavye tarayıcı gerçekleriyle yeniden yazıldı:** `F5`/`F11`/`F12`
  iptal edilemez, `Alt+←/→` Windows ve Linux'ta geri/ileridir.
- **Harf takılamayan paneller kuralı eklendi** (Karar 11) ve
  `Layout.Headed`'in çok panelli anlamı karara bağlandı (Karar 12).
- **Abonelik bütçesi** karara bağlandı: iki `WLA`'lı bir sayfa bugün
  `sub`'ın tamamını düşürür ve istemci hatayı yok sayar.
- **Sıralama gerekçesi düzeltildi:** "`Shell.tsx` başka dalda açık"
  iddiası geçersizdi — dal bu spec yazılmadan önce main'e merge
  edilmişti (`85d6c7b`). 2a-1 ile 2c-1 paralel yürür.
- `CommandKind`, `PageName`, `GroupCommand` enum'ları; panel id
  üretimi, sekme başlığı, dar ekran, dockview tema eşlemesi,
  `Dosyalar`, `Bağımlılıklar`, `Ölçümler` ve bu bölüm eklendi.
