# Web terminal: tarayıcıda klavye odaklı finans terminali

Status: approved, not yet implemented
Date: 2026-09-07
Revised 2026-09-07 after two independent reviews (see "Revizyonlar").
`2026-09-07-observability-design.md` ve
`2026-09-07-pipeline-change-events-design.md` ile kardeş; ikisine de
bağımlı değil. Kesişen iki nokta "Uygulama sırası" altında listelenir.

## Neden

yfin 61 dataset'i PostgreSQL'e yazıyor ve canlı tick akışını arşivliyor,
ama bu veriye bakmanın tek yolu `psql` ya da OAuth2 ile REST çağrısı.
README "outbound socket, so clients subscribe instead of polling, is not
started" diyor. Bu tasarım, self-host eden tek kullanıcının kendi
arşivine tarayıcıdan, Bloomberg alışkanlıklarıyla (`AAPL GP`, `FA`,
`QR`) bakmasını sağlar ve bunu yaparken tarayıcıya canlı tick yayınını
başlatır.

Kapsam bilinçli olarak dar: herkese açık (public) tek terminal, tek
sembol derinliği. Şifreli mod (`YFAPI_UI_PUBLIC=false`) opsiyon olarak
kalır; varsayılan giriş istemez. Watchlist, screener, sürükle-bırak yerleşim ve hosted çok
kiracılılık dışarıda; "Kapsam dışı" bölümü bu spec'in onları
engellemediğini garanti eder.

### Mevcut durum, 2026-09-07'de doğrulandı

| Gerçek | Yer |
| --- | --- |
| FastAPI 0.141.1 kilitli; `app.frontend()` var ama `fallback="auto"` eşleşmeyen her `text/html` isteğine `index.html` döner ve `check_dir="auto"` dizin yoksa `RuntimeError` atar | `uv.lock`, `fastapi/applications.py:1222`, `fastapi/routing.py:1881-1912,1953-1963` |
| Build sistemi setuptools; package-data ile `examples/*.json` gömülüyor; git'i okumaz | `pyproject.toml:82-94` |
| CI committed `openapi.json`'ı byte-byte diff'liyor; kontrat testi yalnızca `/v1`, `/oauth`, `/health` önekli rotaları adlandırmayı zorunlu kılıyor | `.github/workflows/ci.yml:45-46`, `tests/unit/test_api_contract.py:248-261` |
| API `uvicorn --workers 4`, endpoint'ler senkron; `session_factory` senkron | `Dockerfile`, `api/storage/session.py:26` |
| `Principal` frozen dataclass `(client_id, scopes, jti)`; `current_principal` yalnız Bearer | `api/auth/dependencies.py:76-82,151-183` |
| Scope dizeleri `"<family>:read"`; `read_dataset` `entry.scope in principal.scopes` bakar | `core/families.py:39-41`, `api/routers/v1/datasets.py:203` |
| `verify` `audience=settings.jwt_audience` ile doğrular ve `sid`/`epc` iddialarını zorunlu kılar | `api/auth/jwt.py:35,117` |
| `meter` planı `ApiClient`'tan `client_id` ile çözer; `guard()`, `read_dataset`, `list_datasets` hepsi `meter`'dan geçer; `UsageMiddleware` `request.state.limits` yokken hiçbir şey kaydetmez | `api/ratelimit/dependencies.py:72-97,146,188-201`, `ratelimit/policy.py:81-107` |
| Sayfa boyutu `page_size_cap` yoksa `DEFAULT_PAGE_SIZE` | `api/routers/v1/paging.py:28` |
| `_FixedWindow` süreç içi, `/health`'e özel; `test_api_app.py:160` onu `meta` üzerinden monkeypatch'ler | `api/routers/meta.py:45-87` |
| `client_ip` `RequestContextMiddleware`'in `trusted_proxies`'e göre çözdüğü değer; WS istekleri `BaseHTTPMiddleware`'lerden geçmez | `api/core/middleware.py:65-85` |
| Güvenlik başlıkları sabit dict, `setdefault`; CSP yok; `/v1` yanıtları `Cache-Control: private`, `Vary: Authorization` | `api/core/middleware.py:33-40,128-135`, `market.py:139-140` |
| CORS sadece `cors_origins` doluysa; `public_base_url` varsayılan `""` | `api/app.py:118-128`, `api/core/config.py:56` |
| `ApiSettings` env prefix `YFAPI_`; `docker-compose.yml` `api` servisine `YFAPI_*` değerlerini tek tek geçirir | `api/core/config.py:27-28`, `docker-compose.yml:87-98` |
| `.env.example` testi her `ApiSettings` alanının `YFAPI_*` olarak belgelenmesini **zorunlu kılar** ve dosyadaki her anahtarın bir ayar olmasını ister | `tests/unit/test_env_example.py:33-82` |
| `StreamWriter._write` bir batch'i tek transaction'da yazar; `_write_ticks` `(verified_count, unknown)` döner, `accepted` listesi yerel; COPY `ON CONFLICT DO NOTHING` | `stream/writer.py:274-352` |
| Supervisor `archive=false` sembolleri kuyruğa koymaz; `_write` bunları hiç görmez | `stream/supervisor.py:289-290` |
| `live_ticks` PK `(symbol, ts_utc)`; `market_hours_code` ve `received_at` var; `live_quotes` `live_ticks`'in son değer türevi, `quotes_every_n_batches`'te bir yazılır | `models/stream.py:111-181,191-204`, `writer.py:487` |
| API'de Redis senkron istemci; `redis` 8.1.0 kilitli, `redis.asyncio` mevcut; `redis` yalnız `api` extra'sında; stream süreci `import redis` yapmaz | `api/ratelimit/connection.py:22-48`, `pyproject.toml`, grep |
| API süreci çekirdek `Settings`'i `get_settings()` ile okur | `api/storage/session.py:16-22` |
| `bar_gaps` API'de yok ve `NEVER_EXPOSED`'da | `tests/unit/test_api_contract.py:157` |
| `/v1/symbols/{s}` `SymbolDetail.info` taşır; `/v1/symbols?q=` yalnız sembol öneki, `MIN_PREFIX_LENGTH` alt sınırı | `api/schemas/market.py:35-36`, `market.py:202-207` |
| `/v1/symbols/{s}/bars` `interval ∈ 1m,5m,15m,60m,1d,1wk,1mo`, `start/end`, `session=regular|all` (yalnız intraday); `/actions`; `/financials` | `market.py:298-330,435,521`, `models/bars.py:78` |
| `news` `has_symbol=False`; sembol bağı `news_symbols`'ta; generic yüzey join yapmaz | `datasets/news.py:84-112` |
| Analist dataset adları: `recommendations`, `analyst_price_targets`, `upgrades_downgrades`, `earnings_estimate`, `eps_trend` (hepsi `fundamentals` ailesi) | `api/storage/catalog.py` |
| `fakeredis[lua]` ve `pytest-asyncio` `dev` extra'sında; stream bağlantı testleri loopback ws sunucusu kurar | `pyproject.toml`, `tests/unit/test_stream_connection.py` |

### Araştırma, 2026-09-07

Sürümler npm/PyPI'dan okundu.

| Alan | Seçim | Sürüm | Neden, ne reddedildi |
| --- | --- | --- | --- |
| Framework | React 19 + Vite 8 + TypeScript | 19.2 / 8.2 | Aşağıdaki her kütüphanenin birinci sınıf hedefi. Svelte wrapper ister; HTMX çok panelli canlı UI için uygun değil. |
| Grafik | `lightweight-charts` | 5.2.1, Apache-2.0 | Çoklu pane, marker, whitespace verisi, v5.1 data conflation. uPlot daha hızlı ama finans UX'i elle yazılır; Highcharts Stock lisansı riskli. |
| Tablo | kendi `DataTable`'ımız (FA, ANR); `@tanstack/react-virtual` (QR, 1d) | 3.14, MIT | 1b'de `@tanstack/react-table` kullanılmadı: pivot ve beş analist tablosu düz `<table>` ile yeterli. QR'ın sanallaştırması 1d'de. AG Grid faz 1'de gerekmez. |
| Komut paleti | `cmdk` | 1.1, MIT | Düz `<Command>`; `Command.Dialog` kullanılmaz (radix dialog `<style>` enjekte eder, CSP engeller). Kısayollar tek `keydown` dinleyicisi (`react-hotkeys-hook` 1b'de gereksiz görüldü). kbar 1.0 Ağustos 2026'da çıktı, olgunlaşmamış. |
| Durum | `usePanelData` (REST) + `zustand` (1c, canlı store) | zustand 5.0, MIT | REST yükleme kendi hook'umuzda: oturum kapısı, iptal, 401/404/boş durumları tek yerde; `@tanstack/react-query` 1a-1b'de kullanılmadı. Canlı tick store'u 1c'de Zustand seçici abonelik. |
| Yönlendirme | `react-router` | 7.x, MIT | URL ↔ `(symbol, code, args)` eşlemesi ve history tabanlı gezinme. REST yükleme 1a ve 1b'de `usePanelData` hook'u ile (`@tanstack/react-query` kullanılmadı: oturum kapısı, iptal ve 401 akışı tek hook'ta). |
| Canlı taşıma | FastAPI WebSocket + Redis pub/sub | -- | Çift yönlü abonelik; SSE abonelik değişikliği için ayrı REST ister. PG LISTEN/NOTIFY 8 kB sınırı ve bağlantı maliyeti yüzünden reddedildi. Kafka opsiyonel extra, UI için zorunlu kılınmaz. |

Faz 2 yerleşimi için aday `dockview` 8.2 (MIT, `toJSON/fromJSON`);
burada karar bağlamaz, "Kapsam dışı"nda anılır.

Referans projeler: OpenTerminalUI (MIT; React + Zustand + FastAPI +
Redis pub/sub, Ctrl+G GO bar, Ctrl+K palet), OpenBB Workspace
(`widgets.json`, `groupBy` bağlama), Neuberg (FlexLayout, delta WS),
TradingView (layout sync). Bloomberg mnemonikleri: DES, GP, GIP, QR,
FA, ANR, N, CF, EQS, WLA.

## Kararlar

1. **Tek süreç.** SPA mevcut API sürecinden servis edilir; ayrı servis,
   port, container yok.
2. **`app.frontend()` kullanılmaz.** `index.html` elle yazılmış bir
   rotadan, varlıklar `StaticFiles`'tan servis edilir. Gerekçe: FastAPI'nin
   fallback'i eşleşmeyen her `text/html` isteğine, `/v1/typo` dahil,
   200 `index.html` döner ve kamusal API'nin 404 davranışını bozar;
   `check_dir` dizin yoksa başlangıçta patlar; başlık koyma kancası yok.
3. **UI rotaları `/ui` altında, şema dışında.** `/ui`, `/ui/t/*`,
   `/ui/assets/*`, `/ui/api/*`, `/ui/ws`; hepsi `include_in_schema=False`.
   `openapi.json` değişmez.
4. **Çerez oturumu, stateless.** HS256 JWT, mevcut `jwt_signing_key`,
   `aud="yfin-ui"`. Sunucu tarafı oturum tablosu yok; 4 worker sorun
   değil.
5. **UI istekleri ölçülmez.** `client_id="ui"` sentinel; `meter` rate,
   quota ve concurrency'nin üçünü de atlar.
6. **Canlı yol Redis pub/sub, at-most-once, fail-open.** Writer commit
   sonrası yayınlar; Redis yoksa DB yazımı sürer. Kalıcılık Kafka
   yolunun işi.
7. **`redis` ana bağımlılığa taşınır.** `publish.py` stream sürecinde
   koşar; `api` extra'sı orada kurulu olmayabilir. redis-py saf Python,
   maliyeti küçük.
8. **`/v1`'de olmayan üç okuma UI'a özel rotadan gelir:** `bar_gaps`,
   son N tick, sembole göre haber. `/v1`'e taşınmaları `openapi.json`'ı
   değiştirir; o karar ayrı bir değişiklikte alınır.
9. **SameSite=Lax.** Strict, yer iminden ya da dış bağlantıdan gelen ilk
   gezinmede çerezi düşürür ve giriş yapmış kullanıcıya login modalı
   gösterirdi. Lax çapraz site POST'u yine keser.

## Mimari ve dizin yerleşimi

```
web/                          npm paketi, React 19 + Vite + TS, base "/ui/"
  src/app/        shell, router, komut satırı, klavye
  src/commands/   parser, registry, mnemonikler
  src/panels/     DES, GP, GIP, FA, ANR, N, CF, QR, HELP
  src/live/       WS istemcisi, Zustand quote store, rAF coalescing,
                  tick-fields.json (Python'dan üretilir)
  src/api/        REST istemcisi (TanStack Query), tipler
src/yfin/ui/                  yeni Python paketi
  __init__.py     install(app, settings): rotaları ve statik servisi kurar
  router.py       /ui/api/*: login, logout, me, gaps, ticks, news
  pages.py        /ui ve /ui/t/{path:path} → index.html + CSP başlıkları
  session.py      çerez üretimi ve doğrulaması
  live.py         /ui/ws
  static/dist/    Vite çıktısı; .gitignore'da, package-data ile wheel'e
src/yfin/stream/publish.py    tick yayını; alan tablosu tek yerde
src/yfin/api/core/window.py   _FixedWindow buraya taşınır (meta re-export)
```

**Montaj.** `create_app` içinde `settings.ui_enabled` ise
`yfin.ui.install(app, settings)` çağrılır; kapalıyken `yfin.ui` import
edilmez. `install`:

- `/ui/api/*`, `/ui/ws` rotalarını kaydeder.
- `/ui/api/{path:path}` için catch-all 404 problem yanıtı kaydeder; bu
  önekte `index.html` asla dönmez.
- `static/dist` varsa `/ui/assets` altına `StaticFiles` monte eder ve
  `/ui`, `/ui/t/{path:path}` için `index.html` rotasını kaydeder. Dizin
  yoksa (`vite build` koşmamış geliştirme, unit testler) uyarı loglar ve
  yalnızca API/WS rotaları kalır.

**Ayarlar.** `ApiSettings` (`YFAPI_` prefix), hepsi env-only:
`ui_enabled: bool = False`, `ui_public: bool = True`,
`ui_password: str = ""`, `ui_requests_per_minute: int = 600`.
`ui_enabled` açık, `ui_public` kapalı ve şifre boşsa `create_app`
`ValueError` atar; public modda şifre gerekmez. Anahtarlar
`YFAPI_UI_ENABLED`, `YFAPI_UI_PUBLIC`, `YFAPI_UI_PASSWORD`,
`YFAPI_UI_REQUESTS_PER_MINUTE` olarak `.env.example`'a girer; oradaki
test her `ApiSettings` alanının belgelenmesini zorunlu kılar.
`docker-compose.yml` ve README de belgeler.

**Veri yolu: `/ui/api/v1` aynası.** Tarayıcının Bearer token'ı yoktur
ve `/v1` sözleşmesi (`openapi.json`, plan ölçümü) değişmez. Bunun için
`yfin/ui/public.py` `market` ve `datasets` router'larını `/ui/api`'ye
mount edilmiş ikinci bir FastAPI alt uygulamasında yeniden sunar; yollar
`/ui/api/v1/...` olur. Fark yalnızca iki noktadır: `current_principal`
`ui_principal` ile override edilir (public modda sabit `ui` principal,
şifreli modda çerezden) ve `RequestBrake` istemci IP'si başına dakikada
`ui_requests_per_minute` isteği geçirir (aşımı 429 + `Retry-After: 60`).
Alt uygulama OpenAPI belgesi yayınlamaz; `/ui/api` altında
eşleşmeyen her yol onun 404 problem gövdesiyle biter, SPA sayfasına
düşmez. SPA yalnızca bu aynayı ve `/ui/api/*` rotalarını çağırır;
`/v1`'i doğrudan hiç çağırmaz.

**API'ye entegrasyon.** `current_principal`: `Authorization` başlığı
varsa yalnızca o değerlendirilir, geçersizse 401; başlık yoksa ve UI
açıksa `yfin_ui` çerezi denenir. Çerezli istek
`Principal(client_id="ui", scopes=frozenset(scope_for(f) for f in
DataFamily), jti=<çerez jti>)` döner. `meter`, `client_id == "ui"`
görünce `request.state.page_size_cap = UI_PAGE_CAP` (1000) yazar ve
döner; `limits` konmadığı için `UsageMiddleware` ve `attribute_family`
no-op. `/ui/api/*` rotaları yalnız çerez kabul eder (`UiSession`
bağımlılığı); Bearer ile 401.

`/v1` yanıtlarındaki `Vary: Authorization` çerezi kapsamaz; UI istemcisi
`fetch`'i `cache: "no-store"` ile yapar, logout sonrası eski yanıt
görünmez. CORS gerekmez; aynı origin.

**Geliştirme.** Vite dev sunucusu `/ui/api`, `/ui/ws` ve `/v1`'i
`localhost:8000`'e proxy'ler; çerez aynı origin'de kalır.

**Paketleme ve CI.** Vite `src/yfin/ui/static/dist`'e yazar;
`[tool.setuptools.package-data]`'ya `"yfin.ui" = ["static/dist/**"]`.
Dockerfile'a `node:22-slim` build aşaması; runtime imajı `dist`'i
`/app/src/yfin/ui/static/dist`'e kopyalar; builder'ın `COPY src`
layer'ı Node çıktısından bağımsız kalır. CI'a `web` job'u: `npm ci`,
`tsc --noEmit`, `eslint`, `vitest`, `vite build`. Python job'ları
değişmez.

## Komut dili ve panel modeli

**Grammar.** `[SYMBOL] [CODE] [ARGS...]`. Sembol ve fonksiyon token'ları
büyük harfle karşılaştırılır; argüman token'ları `parseArgs`'a olduğu
gibi verilir ve her panel kendi normalizasyonunu yapar (`15m` bir
aralıktır, `15M` değil). Sembol token'ı `/^[A-Z0-9.^=-]+$/` (`BRK-B`,
`^GSPC`, `EURUSD=X`). Çözümleme, üç dallı ve deterministik:

- Tek token, kayıtlı mnemonik → fonksiyon, bağlam sembolü korunur.
- Tek token, mnemonik değil → sembol, panel türü korunur (sembolsüz
  panel açıksa DES).
- İki+ token: ilk ikisi de mnemonikse ilki **sembol** sayılır (`CF DES`
  = CF Industries'in DES'i). İlki mnemonik, ikincisi değilse ilki
  **fonksiyon**, kalanı arg (`GIP 15m`). Aksi hâlde ilk token sembol
  adayı, ikincisi mnemonik olmalı, kalanı arg (`AAPL GIP 1m`).
- `needsSymbol` bir fonksiyon sembolsüz yazılırsa uyarı: "X needs a
  symbol: type one first, e.g. AAPL X".
- Sembol adayı `/v1/symbols/{s}` ile doğrulanır; 404 ise komut satırı
  altında uyarı ve cmdk paleti `/v1/symbols?q=` sonuçlarıyla açılır.
  `q` yalnız sembol öneki eşler; "Apple" ile arama çalışmaz, palet
  bunu belirtir.
- `parseArgs` hata fırlatırsa uyarı; yığına itilmez.

**Faz 1 mnemonikleri**

| Kod | Şablon | Panel | Kaynak |
| --- | --- | --- | --- |
| `DES` | headed | Ad, borsa, sektör, `info` alanlarından temel oranlar; canlı fiyat şeritte | `/v1/symbols/{s}` (tek çağrı; `info` içinde) |
| `GP` | headed | Günlük mum + hacim + temettü/split marker; son 2 yıl | `/bars?interval=1d`, `/actions` |
| `GIP` | headed | Intraday mum; arg `1m/5m/15m/60m`, varsayılan `5m`; son 5 işlem günü; `session=regular`; gap overlay; canlı mum | `/bars`, `/ui/api/symbols/{s}/gaps`, WS |
| `FA` | single | Gelir/bilanço/nakit akışı; yıllık/çeyrek sekmeleri | `/v1/symbols/{s}/financials` |
| `ANR` | single | Tavsiyeler, hedef fiyat, yükseltme/düşürme, kazanç tahmini, EPS trendi | `/v1/datasets/{recommendations, analyst_price_targets, upgrades_downgrades, earnings_estimate, eps_trend}?symbol=` |
| `N` | single | Haber listesi; seçince özet ve link | `/ui/api/symbols/{s}/news` (`news_symbols ⋈ news`, `pub_date` desc) |
| `CF` | single | SEC dosyaları ve ekleri | `/v1/datasets/sec_filings?symbol=`, `sec_filing_exhibits?symbol=` |
| `QR` | headed | Time & sales; sanallaştırılmış | WS; açılışta `/ui/api/symbols/{s}/ticks?limit=` (varsayılan 500, en çok 2000) |
| `HELP` | single | Mnemonik listesi; `needsSymbol=false` | statik |

**Panel sözleşmesi**

```ts
type PanelArgs = Record<string, string>;
interface PanelSpec {
  code: string;
  title: string;
  needsSymbol: boolean;
  layout: "single" | "headed";
  parseArgs(tokens: string[]): PanelArgs;   // throws on invalid
  component: React.FC<{ symbol: string | null; args: PanelArgs }>;
}
```

Paneller gezinme durumunu okumaz; sembolü prop olarak alır. Canlı
store'a yalnızca prop'taki sembol anahtarıyla seçici abone olurlar. Bu,
faz 2'de dockview grup harflerine bağlanmanın ön koşulu.

**Şablonlar.** `single`: komut satırı altında tek panel. `headed`: üstte
ince canlı fiyat şeridi (son tick, değişim, seans durumu), altında
panel. Şablon `PanelSpec`'te sabit; sürükleme yok. 1b'de `layout`
alanı yalnızca tanımlıdır; `Shell` onu 1c'de (canlı şerit gelince)
okumaya başlar.

**Gezinme.** Yığın tarayıcı history'sidir: her komut
`history.pushState`; `Esc` = `back()`, `Shift+Esc` = `forward()`. Komut
kutusu odaktayken `Esc` önce kutuyu kapatır. URL `/ui/t/{SYMBOL}/{CODE}?
{args}`; sembolsüz panel için `/ui/t/-/HELP`, bağlamda sembol varsa
sembol korunur (`AAPL` açıkken `HELP` → `/ui/t/AAPL/HELP`, `Esc` ile
geri dönülünce sembol kaybolmaz). Kök `/ui` isteği
`localStorage["yfin.ui.last"]`'taki üçlüye yönlendirir; yoksa boş DES ve
odak kutuda. `localStorage` yalnız bu yönlendirme içindir; ikinci bir
durum kaynağı değildir.

**Kısayollar.** Ctrl+K (macOS'ta Meta+K) ve `/` kutuya odak; `Esc` /
`Shift+Esc` gezinme; `?` HELP; panel içinde `j`/`k`/`Enter`. Kısayollar
bir input odaktayken pasif.

## Canlı veri yolu

**Yayın noktası.** `_write_ticks`, `accepted` listesini de döndürecek
şekilde değişir. `_write`, `session.commit()` başarılı olduktan sonra
`publish.publish_ticks(accepted)` çağırır. Commit atarsa yayın yok.
`accepted` FK filtresinden geçen satırlardır; `ON CONFLICT DO NOTHING`
ile düşen tekrarlar da içindedir, yani aynı `(s, t)` iki kez
yayınlanabilir. İstemci `QR`'da `(s, t)` tekrarını atar. Reject'ler
yayınlanmaz.

**Sınır.** Supervisor `archive=false` sembolleri writer'a hiç
ulaştırmaz; bu semboller için ne canlı tick ne `live_ticks` geçmişi
vardır, yalnız `live_quotes` `snap`'i çalışır. `stream scope add
--archive` olmayan semboller `QR`'da boş kalır ve şerit "arşivlenmiyor"
yazar. Arşivsiz sembol için yayın faz 1.5'te supervisor'dan
değerlendirilir.

**Redis kanalı ve tick gövdesi.** Kanal `yfin:tick:{SYMBOL}`. Gövde
JSON, alan tablosu `yfin/stream/publish.py`'de tek yerde:

| Anahtar | Tip | Zorunlu | Kaynak sütun |
| --- | --- | --- | --- |
| `s` | string | evet | `symbol` |
| `t` | int, epoch ms UTC | evet | `ts_utc` |
| `p` | string (Decimal) | evet | `price` |
| `mh` | int | evet | `market_hours_code` |
| `c`, `cp`, `h`, `l`, `o`, `pc`, `b`, `a` | string (Decimal) | hayır | `change`, `change_percent`, `day_high`, `day_low`, `open_price`, `previous_close`, `bid`, `ask` |
| `v`, `ls`, `bs`, `as` | int | hayır | `day_volume`, `last_size`, `bid_size`, `ask_size` |

`t` epoch ms, çünkü `canonical_json` datetime'ı isoformat'a çevirir ve
istemci onu parse etmek zorunda kalırdı. Boş alanlar gönderilmez.
Python unit testi bu tabloyu `web/src/live/tick-fields.json`'a yazar
(`--check` modu CI'da, `openapi.json` kalıbı); vitest TS tipinin
anahtarlarını bu dosyayla karşılaştırır.

Yayın, commit sonrası tek `pipeline()` içinde N `PUBLISH`. Sembol
başına tek mesajda dizi seçeneği ölçüme bırakılır; commit öncesi yayın
seçenek değildir.

**Ayarlar.** Çekirdek `Settings`, grup `stream`:
`yf_stream_publish_enabled: bool = False` (DB-managed; `yfin config set`
ile açılır) ve `yf_stream_publish_redis_url: str = ""` (env-only;
kimlik bilgisi taşıyabilir). URL boşken yayın kapalıdır. Her ikisi
`.env.example`'a girer (çekirdek alanlar).

**Fail-open.** `redis` istemcisi tembel açılır. Bağlantı ya da
`publish` hatası: uyarı **durum değişiminde bir kez**, sayaç
`yfin_stream_publish_total{result="ok"|"failed"|"disabled"}` her
batch'te; DB yazımı sürer. Observability spec'ine bu sayaç bir satır
olarak eklenir.

**`/ui/ws`.** El sıkışmada çerez doğrulanır; yoksa/geçersizse kapanış
4401. `Origin` kontrolü: `public_base_url` doluysa origin'i onunla,
boşsa isteğin `Host` başlığıyla (şema bağımsız) eşleşmeli; yoksa ya da
eşleşmiyorsa 4403. İstemci 4401'de yeniden bağlanmaz, login modalını
açar. WS `BaseHTTPMiddleware`'lerden geçmediği için `request_id`
handler'da üretilir ve loglanır.

Çerçeve her zaman `{"op": ..., ...}`:

| Yön | Mesaj |
| --- | --- |
| ↑ | `{"op":"sub","symbols":[...]}` / `{"op":"unsub","symbols":[...]}` |
| ↓ | `{"op":"live","enabled":bool}` bağlantıda bir kez |
| ↓ | `{"op":"snap","d":{tick gövdesi}}` her `sub` sembolü için, `live_quotes`'tan aynı alan tablosuyla |
| ↓ | `{"op":"tick","d":{tick gövdesi}}` |
| ↓ | `{"op":"dropped","n":int}` son `dropped`'tan bu yana düşen |
| ↓ | `{"op":"error","code":"too_many"\|"bad_symbol"}` |

Sunucu bağlantı başına bir `redis.asyncio` pub/sub tutar; senkron
`get_redis`'e dokunulmaz. `sub` sırası: Redis'e abone ol → `snap`
gönder → tick'ler; istemci `t < snap.t` olan tick'i atar. Semboller
büyük harfe çevrilir; bağlantı başına üst sınır 200. `snap` ve `ticks`
sorguları senkron `session_factory` ile `run_in_threadpool` içinde.
`asyncio.Queue(maxsize=1000)`; dolarsa en eski düşer. Redis'e
ulaşılamıyorsa bağlantı kabul edilir, `live.enabled=false`, yalnız
`snap` çalışır. `live_enabled` = API Redis'e ulaşıyor **ve**
`yf_stream_publish_enabled` (ayar tablosundan okunur).

**Tarayıcı.** Gelen tick'ler `Map<symbol, Tick[]>`'e birikir;
`requestAnimationFrame` her karede toplu olarak store'a uygular. Store
sembol başına abone sayısı tutar; sıfıra inince `unsub`; `GP → QR`
geçişi aynı sembolde `snap` tekrarı doğurmaz. Store yalnız `QR`'ın
sembolü için 2000 satırlık halka tutar; diğerleri son tick. Şerit son
tick'i gösterir; `live.enabled=false` ise "canlı akış kapalı, son:
HH:MM" yazar ve `snap`'i 30 s'de bir yineler.

`GIP` mum toplayıcı: kova `floor(t / interval)` UTC; `mh` regular
seans dışındaki tick'ler atılır (`session=regular` ile uyum); her mum
sınırında ve her yeniden bağlanmada son 2 bar REST'ten yenilenir,
kopukluktaki tick'ler doldurulmaz. Zaman dilimi faz 1'de tarayıcı yerel
saati.

Yeniden bağlanma 1 s'den 30 s'e üstel (4401 hariç); bağlanınca
abonelik seti tekrar gönderilir. `QR` kopukluk için ayırıcı satır
gösterir.

## Kimlik doğrulama ve oturum

**Public mod (varsayılan, `YFAPI_UI_PUBLIC=true`).** Giriş yoktur:
`/ui/api/login` ve `/ui/api/logout` 404 döner, `GET /ui/api/me`
`{"authenticated": true, "expires_at": null, "live_enabled": false,
"public": true}` verir ve SPA giriş modalını hiç göstermez. Aşağıdaki
çerez mekanizması yalnızca `YFAPI_UI_PUBLIC=false` iken devrededir; o
modda `me` gövdesi `"public": false` taşır.

**Künye.** Sayfa altbilgisi Yahoo Finance'e (`https://finance.yahoo.com/`)
ve `yfinance` paketine (`https://github.com/ranaroussi/yfinance`)
logolarıyla bağlantı verir ve Yahoo ile bağlantısızlık notunu taşır;
logolar CSP'nin `img-src https:` iznine dayanır.

- `POST /ui/api/login`: form alanı `password`, sabit zamanlı
  karşılaştırma; başarı 204 + `Set-Cookie: yfin_ui`; hata mevcut
  problem gövdesi.
- Çerez JWT claims: `iss` (mevcut), `aud="yfin-ui"`, `iat`, `exp`
  (+24 saat, kayan yenileme yok), `jti` (login'de 128 bit rastgele),
  `pwf` (şifrenin SHA-256 parmak izinin ilk 16 hex karakteri). `sub`
  yok. `session.py` kendi `jwt.decode(audience="yfin-ui",
  options={"require": ["exp","iat","jti","pwf"]})`'unu yazar; mevcut
  `verify` `sid`/`epc` istediği için yeniden kullanılamaz. Mevcut
  `verify` `audience=jwt_audience` doğruladığından UI çerezi Bearer
  olarak reddedilir; ters yön `aud` uyuşmazlığıyla reddedilir. Şifre
  değiştirilince tüm oturumlar anında geçersiz olur (tek operatörün
  sahip olduğu tek olay müdahalesi budur); `jwt_signing_key`
  rotasyonu ise ayrıca her `/v1` token'ını da öldürür.
- `POST /ui/api/logout` çerezi siler. `GET /ui/api/me` her zaman 200:
  `{"authenticated": bool, "expires_at": <UNIX epoch saniye>|null, "live_enabled": bool, "public": bool}`.
- Çerez: `HttpOnly`, `SameSite=Lax`, `Path=/`; `Secure` yalnız
  `public_base_url` https ise. `public_base_url` boşken çerez düz HTTP'de
  gider; self-host tek kullanıcı için kabul edilir ve README'de yazar.
- CSRF token yok: Lax çapraz site POST'u keser; login dışı tek yazma
  logout. Faz 2'de yazma rotaları gelince Origin kontrolü eklenir.
- Kaba kuvvet: `_FixedWindow` `api/core/window.py`'ye taşınır
  (`meta` re-export eder, `test_api_app.py:160` çalışmaya devam eder);
  login `client_ip` başına dakikada 5. Süreç içi, 4 worker'da fiilî 20.
  Reverse proxy arkasında `YFAPI_TRUSTED_PROXIES` boşsa `client_ip` proxy
  IP'sidir ve sınır **toplam** olur; README bunu UI için zorunlu
  yapılandırma olarak yazar.
- Şifre env'de düz metin. Tek kullanıcı için hash'lemek çözülen sorun
  yaratmaz; hosted fazında kullanıcı tablosuyla değişir.
- `pages.py` `index.html`'i döndürürken başlıkları koyar:
  `Content-Security-Policy: default-src 'self'; connect-src 'self';
  img-src 'self' data: https:; style-src 'self'; frame-ancestors 'none';
  base-uri 'none'; form-action 'self'` ve
  `X-Frame-Options: DENY`. `connect-src 'self'` aynı origin WS'i kapsar.
  lightweight-charts ve cmdk'nın inline stil gereksinimi 1a'da
  doğrulanır; gerekiyorsa `style-src` nonce ile açılır. Middleware
  `setdefault` kullandığından üzerine yazmaz.

## Dosyalar

**Yeni:** `web/` (package.json, vite.config.ts, tsconfig.json,
eslint.config.js, src/...), `src/yfin/ui/{__init__,router,pages,
session,live}.py`, `src/yfin/stream/publish.py`,
`src/yfin/api/core/window.py`, `web/src/live/tick-fields.json`
(üretilir, commit edilir).

**Değişen:** `api/app.py` (koşullu `ui.install`), `api/core/config.py`
(iki alan), `api/auth/dependencies.py` (çerez dalı),
`api/ratelimit/dependencies.py` (`ui` erken dönüşü),
`api/routers/meta.py` (`_FixedWindow` re-export), `stream/writer.py`
(`accepted` dönüşü, commit sonrası yayın), `core/config.py` (iki
`stream` alanı), `pyproject.toml` (`redis` ana bağımlılık, package-data),
`.gitignore` (`src/yfin/ui/static/dist`), `Dockerfile` (node aşaması),
`.github/workflows/ci.yml` (`web` job'u), `.env.example`
(`YFAPI_UI_*`, `YF_STREAM_PUBLISH_*`), `docker-compose.yml` (`api`: `YFAPI_UI_ENABLED`,
`YFAPI_UI_PASSWORD`, `YFAPI_PUBLIC_BASE_URL`; stream servisi varsa
`YF_STREAM_PUBLISH_REDIS_URL`), `README.md`.

**Bağımlılıklar.** Runtime: `redis` `api` extra'sından ana listeye.
Dev: `fakeredis` zaten var; `web/devDependencies`'e Playwright (1d'de).

## Uygulama sırası

| # | Alt proje | Çıktı | Bağımlılık |
| --- | --- | --- | --- |
| 1a | İskelet | `web/` paketi, Vite build ve dev proxy, `yfin.ui` montajı, login/çerez/`me`, `pages.py` + CSP, `meter` erken dönüşü, CI web job'u, Dockerfile node aşaması, `DES` paneli (URL ile sembol, şerit yalnız sembol adı) | yok |
| 1b | Komut dili | parser, registry, cmdk, history gezinme, `HELP`, `FA`, `ANR`, `N` (+`/ui/api/.../news`), `CF` | 1a |
| 1c | Canlı yol | `stream/publish.py`, iki `stream` ayarı, `/ui/ws`, `/ui/api/.../ticks`, WS istemcisi ve store, şeridin canlı hâli, ölçüm | 1a |
| 1d | Grafikler ve QR | lightweight-charts, `GP`, `GIP`, marker, gap overlay, `/ui/api/.../gaps`, canlı mum, `QR` paneli, Playwright senaryosu | 1b, 1c |

1b ve 1c bağımsız, paralel yürütülebilir. `QR` bir `PanelSpec` olduğu
için 1d'dedir. Her alt proje kendi implementation plan'ını alır.

**Kardeş spec'lerle kesişme.** Observability spec'i de `_FixedWindow`'u
`api/core/window.py`'ye taşıyor ve `meta.py`'yi düzenliyor: ikinci gelen
rebase eder. `stream/writer.py`'deki yayın kancası observability'nin
sayaç listesine `yfin_stream_publish_total` olarak eklenir.

**1a'daki spike.** `UsageMiddleware`'in `limits` yokken kayıt yapmadığı
kod okumasıyla doğrulandı (`dependencies.py:188-201`); 1a bunu testle
kilitler.

## Kapsam dışı

**Faz 1.5:** `EQS`, `WLA`, `ECO`/`ERN`/`IPO` takvimleri (yeni
`PanelSpec` kayıtları); arşivsiz semboller için yayın; 100+ sembol
aboneliği ölçümü (`WLA` kapısı); borsa zaman dilimi.

**Faz 2:** dockview yerleşim, A/B/C grup harfleri, kayıtlı sayfalar ve
F-tuşları, layout'un DB'de tutulması, arşiv oynatma (replay), hosted
kullanıcı tablosu. Faz 1'in bunları engellemediği üç garanti: paneller
sembolü prop olarak alır; `PanelSpec` yerleşim bilgisi taşımaz, yalnızca
şablon tercihi; WS abonelik seti bağlantı başına ve referans sayımlı.

## Ölçümler

1c "bitti" sayılmadan `docs/measurements/websocket.md`'ye eklenir: batch
commit'ten tarayıcıda ekrana gecikme (p50/p99, tek sembol). Sonuç,
yayının N `PUBLISH` mi sembol başına dizi mi olacağını belirler.

## Hata yönetimi

- API 5xx: panelde hata kartı ve Retry düğmesi; otomatik yeniden deneme
  yok. Komut satırı çalışır.
- Sembol 404: uyarı ve palet; bağlam değişmez.
- Dataset boş: "veri yok". `sync_run_items` durumu API'de olmadığından
  faz 1'de gösterilmez; eksiklik burada kayıtlı.
- 401 (REST) ya da 4401 (WS): istekler durur, login modalı, başarılı
  login sonrası son komut yeniden koşar ve WS yeniden bağlanır.
- WS kopması: şeritte kırmızı nokta, `QR`'da ayırıcı; REST panelleri
  etkilenmez.
- `live.enabled=false`: şerit "canlı akış kapalı, son: HH:MM", `snap`
  30 s'de bir yinelenir.
- `/ui/api` bilinmeyen yol: 404 problem gövdesi, asla `index.html`.

## Testler

**`tests/unit`:** çerez üretimi/doğrulama, claims zorunluluğu; Bearer
varsa çereze düşülmemesi; çerez JWT'si Bearer olarak 401, Bearer JWT'si
çerez olarak 401; `ui_enabled` + boş şifre → `ValueError`; UI
kapalıyken `/ui/*` yok ve `yfin.ui` import edilmemiş; `dist` yokken
montajın SPA'yı atlaması; `/ui/api/bilinmeyen` → 404 problem; login
rate limit; `meter` `ui` için rate/quota/concurrency atlar ve
`UsageMiddleware` kaydetmez; `page_size_cap` 1000; `publish` alan
tablosu ve `tick-fields.json --check`; fail-open (Redis mock atarsa DB
yazımı sürer, uyarı bir kez); `openapi.json` değişmemiş; `Secure`
bayrağı koşulu; CSP başlıkları `index.html` yanıtında.

**`tests/repo`:** `snap` sorgusu `live_quotes`'tan doğru satır;
`_write` commit sonrası yayın sırası ve `accepted` içeriği (fakeredis);
`/ui/api/.../news` join'i; `gaps` yalnız açık gap'ler.

**WS:** `pytest-asyncio`, TestClient websocket, fakeredis pub/sub:
4401/4403 kapanışları, `sub → snap → tick` sırası, `too_many`, kuyruk
taşması → `dropped`, Redis yokken `live.enabled=false`.

**`web/` vitest:** parser (tablo odaklı: girdi → üçlü, `CF DES`
belirsizliği dahil), history gezinme, rAF coalescing (sahte
zamanlayıcı), abonelik referans sayımı, tick → OHLC toplayıcı (kova,
`mh` filtresi), `(s,t)` tekrar atma, `tick-fields.json` ile tip
anahtarları. React Testing Library: sembol prop'u değişince yeniden
sorgu; `dropped` görünür.

**E2E:** CI'da koşmaz; 1d'nin yerel kabul ölçütü olarak tek Playwright
senaryosu: login → `AAPL GIP 5m` → mum görünür.

## Revizyonlar

2026-09-07, iki bağımsız inceleme sonrası:

- `app.frontend()` bırakıldı; fallback ve `check_dir` davranışı
  nedeniyle elle rota (Karar 2).
- `redis` ana bağımlılığa; yayın için ayrı Redis URL ayarı (Karar 7).
- `.env.example` testi her `ApiSettings` alanını zorunlu kıldığı için
  UI anahtarları oraya girer (ilk inceleme eski dosyayı okumuştu).
- `_write_ticks` `accepted`'ı döndürür; tekrar yayın olasılığı ve
  istemci tarafı tekrar atma yazıldı.
- `archive=false` sembollerin canlı yolu olmadığı yazıldı.
- `news` sembolle filtrelenemediği için `/ui/api/.../news`; `DES` tek
  çağrı; `ANR` dataset adları; `GP`/`GIP` aralık ve `session` değerleri.
- Scope dizeleri `scope_for(f)`; Bearer öncelik kuralı; `/ui/api` yalnız
  çerez.
- SameSite Strict → Lax; Origin kontrolü `Host` yedeği; kapanış kodları;
  login limitinin `trusted_proxies` bağımlılığı.
- Tick gövdesi alan tablosu, `t` epoch ms, `mh` alanı, `op` zarfı, `snap`
  eşlemesi, `sub` sırası, 200 sembol sınırı, `dropped` anlamı,
  `run_in_threadpool`.
- Parser grammar ve `CF DES` kuralı; history tabanlı gezinme; `HELP`
  URL'i; `localStorage` yalnız yönlendirme.
- `QR` 1d'ye; E2E ifadesi; test listesi tasarım kararlarını kapsayacak
  şekilde genişletildi.
- 100 sembol CPU ölçümü faz 1.5'e; `HP`/`EE` listeden çıkarıldı;
  dockview araştırma tablosundan "Kapsam dışı"na.
