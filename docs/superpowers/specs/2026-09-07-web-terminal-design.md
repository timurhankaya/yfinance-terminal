# Web terminal: tarayıcıda klavye odaklı finans terminali

Status: approved, not yet implemented
Date: 2026-09-07
`2026-09-07-observability-design.md` ve
`2026-09-07-pipeline-change-events-design.md` ile kardeş; ikisine de
bağımlı değil. Kesişen tek nokta `stream/writer.py`'ye eklenen yayın
kancası; observability tasarımı bu kancayı da sayaçlar.

## Neden

yfin 61 dataset'i PostgreSQL'e yazıyor ve canlı tick akışını arşivliyor,
ama bu veriye bakmanın tek yolu `psql` ya da OAuth2 ile REST çağrısı.
README "outbound socket, so clients subscribe instead of polling, is not
started" diyor. Bu tasarım, self-host eden tek kullanıcının kendi
arşivine tarayıcıdan, Bloomberg alışkanlıklarıyla (`AAPL GP`, `FA`,
`QR`) bakmasını sağlar ve bunu yaparken tarayıcıya canlı tick yayınını
başlatır.

Kapsam bilinçli olarak dar: tek kullanıcı, tek şifre, tek sembol
derinliği. Watchlist, screener, sürükle-bırak yerleşim ve hosted çok
kiracılılık bu spec'in dışında; "Faz sınırları" bölümü bu spec'in onları
engellemediğini garanti eder.

### Mevcut durum, 2026-09-07'de doğrulandı

| Gerçek | Yer |
| --- | --- |
| FastAPI 0.141.1 kilitli; `app.frontend()` mevcut | `uv.lock`, `fastapi/applications.py:1222` |
| Build sistemi setuptools; package-data ile `examples/*.json` gömülüyor | `pyproject.toml:82-94` |
| CI committed `openapi.json`'ı byte-byte diff'liyor | `.github/workflows/ci.yml:45-46`, `tests/unit/test_api_contract.py` |
| API `uvicorn --workers 4`, endpoint'ler senkron | `Dockerfile` |
| `current_principal` sadece Bearer kabul ediyor; `Principal(client_id, scopes, jti)`, plan alanı yok | `api/auth/dependencies.py:77-82,151-186` |
| `meter` planı `ApiClient` tablosundan `client_id` ile çözer; satır yoksa `plan_row_missing` loglar | `api/ratelimit/dependencies.py:72-97`, `ratelimit/policy.py:81-106` |
| `_FixedWindow` süreç içi sayaç, `/health`'e özel | `api/routers/meta.py:45-87` |
| Güvenlik başlıkları sabit bir dict, `setdefault` ile konur; CSP yok | `api/core/middleware.py:33-40,128-135` |
| CORS sadece `cors_origins` doluysa | `api/app.py:118-128` |
| `StreamWriter._write` bir batch'i tek transaction'da yazar; Redis'e dokunmaz | `stream/writer.py:274-291` |
| API'de Redis senkron istemci (`redis.Redis.from_url`) | `api/ratelimit/connection.py:22-47` |
| `redis` `api` extra'sında, `websockets` ana bağımlılıkta | `pyproject.toml` |
| `bar_gaps` API'de yok | `api/routers/v1/*`, `api/storage/catalog.py` (grep) |
| `/v1/symbols/{s}/bars` interval/start/end/session alır; `/financials`, `/actions`, `/datasets/{name}?symbol=` var | `api/routers/v1/market.py:285-330,421,506`, `datasets.py:154` |
| `.env.example` her env-only alanı belgelemek zorunda | `tests/unit/test_env_example.py` |
| Stream bağlantı testleri loopback websocket sunucusu kuruyor, `pytest-asyncio` | `pyproject.toml` dev extra notu |

### Araştırma, 2026-09-07

Sürümler npm/PyPI'dan okundu.

| Alan | Seçim | Sürüm | Neden, ne reddedildi |
| --- | --- | --- | --- |
| Framework | React 19 + Vite 8 + TypeScript | 19.2 / 8.2 | Aşağıdaki her kütüphanenin birinci sınıf hedefi. Svelte wrapper ister; HTMX çok panelli canlı UI için uygun değil. |
| Grafik | `lightweight-charts` | 5.2.1, Apache-2.0 | Çoklu pane, marker, whitespace verisi, v5.1 data conflation. uPlot daha hızlı ama finans UX'i elle yazılır; Highcharts Stock lisansı riskli. |
| Tablo | `@tanstack/react-table` + `react-virtual` | 9.2 / 3.14, MIT | Headless, akan liste (`QR`) için tam kontrol. AG Grid Community faz 1'de gerekmez. |
| Komut paleti | `cmdk` + `react-hotkeys-hook` | 1.1 / 5.3, MIT | Scope'lu kısayol; kbar 1.0 yeni. |
| Durum | `@tanstack/react-query` + `zustand` | 5.102 / 5.0, MIT | REST için Query, canlı store için Zustand seçici abonelik. |
| Faz 2 yerleşim | `dockview` | 8.2, MIT | Sıfır bağımlılık, `toJSON/fromJSON`, popout. golden-layout 2022'den beri bakımsız. |
| Canlı taşıma | FastAPI WebSocket + Redis pub/sub | -- | Çift yönlü abonelik; SSE abonelik değişikliği için ayrı REST ister. PG LISTEN/NOTIFY 8 kB sınırı ve bağlantı maliyeti yüzünden reddedildi. Kafka opsiyonel extra, UI için zorunlu kılınmaz. |

Referans projeler: OpenTerminalUI (MIT; React + Zustand + FastAPI +
Redis pub/sub, Ctrl+G GO bar, Ctrl+K palet), OpenBB Workspace
(`widgets.json`, `groupBy` bağlama), Neuberg (FlexLayout, delta WS),
TradingView (layout sync). Bloomberg mnemonikleri: DES, GP, GIP, HP, QR,
FA, ANR, EE, N, CF, EQS, WLA.

## Bölüm 1: Mimari ve dizin yerleşimi

**Tek süreç, iki yüz.** SPA mevcut API sürecinden `app.frontend()` ile
servis edilir. Ayrı servis, port, container yok; self-host için bir
compose servisi daha işletme yükü demek.

```
web/                          npm paketi, React 19 + Vite + TS
  src/app/        shell, router, komut satırı, klavye
  src/commands/   parser, registry, mnemonikler
  src/panels/     DES, GP, GIP, FA, ANR, N, CF, QR, HELP
  src/live/       WS istemcisi, Zustand quote store, rAF coalescing
  src/api/        REST istemcisi (TanStack Query), tipler
src/yfin/ui/                  yeni Python paketi
  router.py       /ui/api/*: login, logout, me, gaps
  session.py      imzalı çerez (PyJWT HS256, aud=yfin-ui)
  live.py         /ui/ws: Redis pub/sub aboneliği ve fan-out
  static/dist/    Vite çıktısı; .gitignore'da, package-data ile wheel'e
src/yfin/stream/publish.py    writer'dan Redis'e tick yayını, fail-open
```

**API'ye entegrasyon**

- `ApiSettings`'e env-only `ui_enabled: bool = False` ve
  `ui_password: str = ""`. `ui_enabled` açık ve şifre boşsa `create_app`
  başlangıçta hata verir. UI kapalıyken hiçbir UI modülü import edilmez;
  Python job'ları `dist`'e ihtiyaç duymaz.
- UI rotaları `/ui/api/...` ve `/ui/ws`, hepsi `include_in_schema=False`.
  Committed `openapi.json` değişmez. UI rotaları kamusal sözleşmenin
  parçası değildir; CI diff'i kırmızıya dönerse bir rota sızmıştır.
- Veri için mevcut `/v1/...` rotaları kullanılır. `current_principal`
  önce Bearer'a bakar; yoksa ve UI açıksa `yfin_ui` çerezine bakar.
  Çerezli istek `Principal(client_id="ui", scopes=<tüm DataFamily>,
  jti=<oturum id>)` döner. `client_id == "ui"` sabit sentinel'dir;
  `ApiClient` tablosunda satırı yoktur ve olmamalıdır.
- `meter`, `client_id == "ui"` görünce planı aramaz, limiter'ı
  çağırmaz, `request.state.limits`'i koymaz; sadece
  `request.state.page_size_cap = UI_PAGE_CAP` (sabit 1000) yazar ve
  döner. `UsageMiddleware`'in `limits` yokken hiçbir şey kaydetmediği
  1a'da testle doğrulanır; kaydediyorsa oraya da aynı erken dönüş
  eklenir.
- CORS gerekmez; aynı origin. `cors_origins` davranışı değişmez.

**Paketleme ve CI**

- Vite `web/`'den `src/yfin/ui/static/dist`'e yazar;
  `[tool.setuptools.package-data]`'ya `"yfin.ui" = ["static/dist/**"]`.
- Dockerfile'a `node:22-slim` build aşaması; runtime yalnızca `dist`'i
  kopyalar.
- CI'a `web` job'u: `npm ci`, `tsc --noEmit`, `eslint`, `vitest`,
  `vite build`. Mevcut Python job'ları değişmez, Node'a bağımlı olmaz.

**Dört worker.** Oturum çerezi imzalı ve kendine yeterli; sunucu tarafı
oturum tablosu yok. WS hangi worker'a düşerse o worker Redis'e abone
olur.

## Bölüm 2: Komut dili ve panel modeli

**Sözdizimi.** Üstte tek kutu; Ctrl+K veya `/` odağı oraya getirir.

| Girdi | Anlam |
| --- | --- |
| `AAPL` | Sembol bağlamını değiştir, panel türünü koru (ilk açılışta DES) |
| `AAPL GP` | Sembol ve fonksiyon birlikte |
| `FA` | Bağlamdaki sembolde fonksiyon değiştir |
| `GIP 5m` | Fonksiyona argüman |

Parser deterministik: ilk kelime kayıtlı mnemonik ise fonksiyon, değilse
sembol adayı. Aday `/v1/symbols/{s}` ile doğrulanır; 404 ise cmdk paleti
`/v1/symbols?q=` sonuçlarıyla açılır. Tahmin yok.

**Faz 1 mnemonikleri**

| Kod | Panel | Kaynak |
| --- | --- | --- |
| `DES` | Ad, borsa, sektör, canlı fiyat başlığı, temel oranlar | `/v1/symbols/{s}`, `/v1/datasets/info?symbol=` |
| `GP` | Günlük mum + hacim + temettü/split marker | `/v1/symbols/{s}/bars?interval=1d`, `/actions` |
| `GIP` | Intraday mum; arg 1m/5m/15m/60m; gap overlay; canlı mum | `/bars?interval=…`, `/ui/api/symbols/{s}/gaps`, WS |
| `FA` | Gelir/bilanço/nakit akışı; yıllık/çeyrek sekmeleri | `/v1/symbols/{s}/financials` |
| `ANR` | Tavsiyeler, hedef fiyat, yükseltme/düşürme | analist dataset'leri |
| `N` | Haber listesi; seçince özet ve link | `/v1/datasets/news?symbol=` |
| `CF` | SEC dosyaları ve ekleri | `sec_filings`, `sec_filing_exhibits` |
| `QR` | Time & sales, canlı tick, sanallaştırılmış | WS; açılışta `live_ticks`'ten son N (`/ui/api/symbols/{s}/ticks`) |
| `HELP` | Mnemonik listesi | statik |

`bar_gaps` ve son tick'ler `/v1`'de olmadığı için `/ui/api` altında
UI'a özel iki okuma rotası açılır. `/v1`'e taşınmaları `openapi.json`'ı
değiştirir; bu kararı ayrı bir değişiklik olarak alınır, burada
verilmez.

**Panel sözleşmesi**

```ts
interface PanelSpec {
  code: string;                 // "GIP"
  title: string;
  needsSymbol: boolean;
  layout: "single" | "headed"; // faz 1'in iki şablonu
  parseArgs(tokens: string[]): PanelArgs;
  component: React.FC<{ symbol: string; args: PanelArgs }>;
}
```

Paneller sembolü prop olarak alır, global durumu okumaz. Bu, faz 2'de
dockview grup harflerine bağlanmanın ön koşulu.

**Şablonlar.** İki sabit şablon: `single` (komut satırı altında tek
panel) ve `headed` (üstte ince canlı fiyat şeridi, altında panel; `GP`,
`GIP`, `QR`, `DES`). Şablon `PanelSpec`'te sabit; sürükleme yok.

**Gezinme.** Her komut `(symbol, code, args)` üçlüsünü yığına iter.
`Esc` geri, `Shift+Esc` ileri. URL üçlüyü taşır
(`/t/AAPL/GIP?interval=5m`); yenileme ve yer imi çalışır. Son üçlü
`localStorage`'da. Kısayollar: Ctrl+K kutu, Esc geri, `?` HELP, panel
içinde `j`/`k`/`Enter`.

## Bölüm 3: Canlı veri yolu

**Yayın noktası.** `StreamWriter._write`, `session.commit()` başarılı
olduktan sonra `publish.publish_ticks(written_rows)` çağırır. Commit
atarsa yayın yok; tarayıcı DB'de olmayan tick görmez. Reject'ler
yayınlanmaz.

**Kanal ve mesaj.** Redis pub/sub, `yfin:tick:{SYMBOL}`. JSON,
`canonical_json`, dolu alanlar, kısa anahtarlar:

```json
{"s":"AAPL","t":"2026-09-07T14:30:01.234Z","p":"231.15","c":"0.42",
 "cp":"0.18","v":12345678,"ls":100,"b":"231.14","a":"231.16",
 "bs":300,"as":200,"h":"232.0","l":"229.8"}
```

Alan eşlemesi `yfin.stream.publish` içinde tek yerde; TypeScript tipi
aynı tablodan üretilir (elle, testle senkron tutulur).

**Fail-open.** Redis yok ya da `publish` atarsa: uyarı logu, sayaç, DB
yazımı sürer. `yf_stream_publish_enabled` ayarı, varsayılan `False`.
Garanti at-most-once; kalıcılık Kafka yolunun işi.

**`/ui/ws`.** Çerezle doğrulanır; `Origin` `public_base_url` ya da
localhost ile eşleşmezse reddedilir. İstemci:

```json
{"op":"sub","symbols":["AAPL"]}   {"op":"unsub","symbols":["AAPL"]}
```

Sunucu bağlantı başına bir `redis.asyncio` pub/sub tutar; senkron
`get_redis` dokunulmaz. `asyncio.Queue(maxsize=1000)`; dolarsa en eski
düşer ve `{"op":"dropped","n":…}` gider. `sub` alınca her sembol için
`live_quotes`'tan son satır `{"op":"snap",…}` olarak gönderilir. Redis
yoksa bağlantı kabul edilir, `{"op":"live","enabled":false}` bildirilir,
sadece `snap` çalışır.

**Tarayıcı.** Gelen tick'ler `Map<symbol, Tick[]>`'e birikir;
`requestAnimationFrame` her karede toplu olarak store'a uygular. Fiyat
şeridi son tick'i, `QR` son 2000 satırı, `GIP` açık mumu (`series.update`,
mum sınırında yeni mum; aralık hesabı istemcide) dinler. Yeniden
bağlanma 1s'den 30s'e üstel; bağlanınca abonelik seti tekrar gönderilir,
`snap` ile tazelenir. Kopukluktaki tick'ler doldurulmaz; `QR` ayırıcı
satır gösterir.

**Ölçüm.** 1c "bitti" sayılmadan `docs/measurements/websocket.md`'ye
eklenir: batch commit'ten ekrana gecikme (p50/p99) ve 100 sembol
abonelikte tarayıcı CPU'su. Sonuç, yayının batch sonrası toplu mu satır
başına mı olacağını belirler; varsayım batch sonrası toplu.

## Bölüm 4: Kimlik doğrulama ve oturum

- `POST /ui/api/login`: form body'de şifre, sabit zamanlı karşılaştırma,
  `yfin_ui` çerezi. İçerik HS256 JWT, mevcut `jwt_signing_key`,
  `aud="yfin-ui"`; böylece UI çerezi Bearer olarak, API token'ı çerez
  olarak geçmez. Süre 24 saat, kayan yenileme yok.
- `POST /ui/api/logout` çerezi siler. `GET /ui/api/me` oturum durumunu
  ve `live_enabled`'ı döner.
- Çerez: `HttpOnly`, `SameSite=Strict`, `Path=/`; `Secure` yalnızca
  `public_base_url` https ise.
- CSRF token yok: SameSite=Strict, login dışı tek yazma logout. Faz 2'de
  yazma rotaları gelince Origin kontrolü eklenir. WS için Origin kontrolü
  faz 1'de var.
- Kaba kuvvet: `_FixedWindow` `api/core`'a taşınır; login IP başına
  dakikada 5. Süreç içi sayaç, 4 worker'da fiilî 20; kabul.
- Şifre env'de düz metin. Tek kullanıcı için hash'lemek çözülen sorun
  yaratmaz; hosted fazında kullanıcı tablosuyla değişir.
- SPA `index.html` yanıtı kendi CSP'sini koyar (`default-src 'self';
  connect-src 'self' ws: wss:; img-src 'self' data:`). Middleware
  `setdefault` kullandığından üzerine yazmaz; middleware değişmez.

## Bölüm 5: Faz bölümlemesi

| # | Alt proje | Çıktı | Bağımlılık |
| --- | --- | --- | --- |
| 1a | İskelet | `web/` paketi, Vite build, `src/yfin/ui` router, login/çerez, `app.frontend()`, CI web job'u, Dockerfile node aşaması, `DES` (statik veri) | yok |
| 1b | Komut dili | parser, registry, cmdk, gezinme yığını, URL, `HELP`, `FA`, `ANR`, `N`, `CF` | 1a |
| 1c | Canlı yol | `stream/publish.py`, `yf_stream_publish_enabled`, `/ui/ws`, WS istemcisi ve store, fiyat şeridi, `QR`, `/ui/api/.../ticks`, ölçüm | 1a |
| 1d | Grafikler | lightweight-charts, `GP`, `GIP`, marker, gap overlay, `/ui/api/.../gaps`, canlı mum | 1b, 1c |

1b ve 1c bağımsız, paralel yürütülebilir. Her alt proje kendi
implementation plan'ını alır.

**Değişen mevcut dosyalar:** `api/app.py`, `api/core/config.py`,
`api/auth/dependencies.py`, `api/ratelimit/dependencies.py`,
`api/routers/meta.py` (`_FixedWindow` taşınır), `stream/writer.py`,
`core/config.py`, `pyproject.toml`, `Dockerfile`,
`.github/workflows/ci.yml`, `.env.example`, `docker-compose.yml`,
`README.md`. Yeni Python bağımlılığı yok; `api` extra'sı UI'ı kapsar.

### Faz sınırları

**Faz 1.5, bu spec'in dışı:** `EQS`, `WLA`, `ECO`/`ERN`/`IPO`
takvimleri. Hepsi yeni `PanelSpec` kayıtları. Watchlist için tek ek:
100+ sembol aboneliği, 1c ölçümüyle doğrulanır.

**Faz 2, bu spec'in dışı:** dockview yerleşim, A/B/C grup harfleri,
kayıtlı sayfalar ve F-tuşları, layout'un DB'de tutulması, arşiv oynatma
(replay). Faz 1'in bunları engellemediği üç garanti: paneller sembolü
prop olarak alır; `PanelSpec` yerleşim bilgisi taşımaz, yalnızca şablon
tercihi; WS abonelik seti bağlantı başına tutulur, çoklu panel aynı
sembolü paylaşabilir.

## Bölüm 6: Hata yönetimi ve test

**Hata yönetimi**

- API 5xx: panelde hata kartı ve retry; TanStack Query 3 deneme. Komut
  satırı çalışır.
- Sembol 404: komut satırı altında uyarı, cmdk arama listesi; bağlam
  değişmez.
- Dataset boş: "veri yok". `sync_run_items` durumu API'de olmadığından
  faz 1'de gösterilmez; eksiklik burada kayıtlı.
- 401: istekler durur, login modalı, başarılı login sonrası son komut
  yeniden koşar.
- WS kopması: şeritte kırmızı nokta, `QR`'da ayırıcı; REST panelleri
  etkilenmez.
- Yayın kapalı: `/ui/api/me` bildirir, şerit "canlı akış kapalı", `snap`
  yine gelir.

**Testler**

- `tests/unit`: çerez üretimi/doğrulama, Bearer ve çerez sırası, `aud`
  ayrımı, login rate limit, `publish` şeması, fail-open, `meter`'ın
  `ui` için limiter'ı atlaması ve `UsageMiddleware`'in kaydetmemesi,
  UI rotalarının `openapi.json`'da olmaması.
- `tests/repo`: `snap` sorgusu; `_write` commit sonrası yayın sırası
  (fakeredis).
- WS: `pytest-asyncio`, TestClient websocket, fakeredis pub/sub.
- `web/` vitest: parser (tablo odaklı), gezinme yığını, rAF coalescing
  (sahte zamanlayıcı), tick → OHLC toplayıcı. React Testing Library:
  sembol prop'u değişince yeniden sorgu; `dropped` görünür.
- E2E faz 1'de yok; 1d bitince Playwright ile tek senaryo: login →
  `AAPL GIP 5m` → mum görünür.

**CI kapısı:** web job'u `tsc`, eslint, vitest, `vite build`.
`openapi.json` diff'i değişmeden yeşil kalmalı.
