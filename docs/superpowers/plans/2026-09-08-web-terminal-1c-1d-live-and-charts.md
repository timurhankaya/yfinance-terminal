# Web Terminal 1c + 1d: Canlı Yol ve Grafikler — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Writer'ın commit ettiği her tick tarayıcıya ulaşsın (`1c`), ve arşivin mumları
`GP`/`GIP` ile çizilsin, canlı tick son mumu yerinde güncellesin, `QR` time & sales aksın (`1d`).

**Architecture:** Yayın noktası `StreamWriter._write`'ın commit'inden **sonra**: `publish.py`
tek alan tablosuyla `yfin:tick:{SYMBOL}` kanallarına JSON basar, fail-open. API süreci
`/ui/ws`'te bağlantı başına bir `redis.asyncio` pub/sub tutar; `sub` → `snap` (`live_quotes`) →
`tick`. Tarayıcıda tek Zustand store: gelen tick'ler `requestAnimationFrame` ile toplu uygulanır,
sembol başına referans sayımlı abonelik, yalnız `QR`'ın sembolü için 2000 satırlık halka.
Grafikler `lightweight-charts` 5.2.1; saf dönüşümler (`bars → candles`, `actions → markers`,
`gaps → whitespace + band`, `ticks → son mum`) React'ten ayrı bir modülde durur ki jsdom'da
canvas olmadan test edilebilsin.

**Tech Stack:** Python 3.13 / FastAPI WebSocket / `redis.asyncio` 8.1; React 19,
`lightweight-charts` 5.2.1, `zustand` 5.0, `@tanstack/react-virtual` 3.14 (yeni bağımlılıklar);
vitest 4 + RTL; Playwright (yalnız yerel).

**Spec:** `docs/superpowers/specs/2026-09-07-web-terminal-design.md` — "Canlı veri yolu",
"Faz 1 mnemonikleri" (`GP`, `GIP`, `QR`), "Uygulama sırası" (1c ve 1d satırları), "Testler".

## Global Constraints

- Committed `openapi.json` **değişmez**: `/ui/ws`, `/ui/api/.../ticks`, `/ui/api/.../gaps`
  hepsi `include_in_schema=False`. Her task sonunda `uv run python scripts/dump_openapi.py --check`.
- Python: `uv run ruff check . && uv run mypy src/yfin && uv run pytest -q tests/unit`.
- Web: `cd web && npm run check && npm run lint && npm test && npm run build`.
- Bağımlılık eklerken plain `npm install` çalıştırılmaz: `npm install --package-lock-only <pkg>@<sürüm>`
  sonra `npm ci`.
- Inline `style=` yok (CSP `style-src 'self'`). lightweight-charts kendi stillerini CSSOM
  üzerinden yazar (`element.style.x = v`), bu CSP'ye takılmaz; `<style>` enjekte etmez.
- **Zaman dilimi: UTC.** Grafik ekseni, tooltip ve `QR` saatleri UTC'dir ve etiketi yazılır.
  Spec'in "faz 1'de tarayıcı yerel saati" ifadesi bu planla değişir (Task 12): terminalin
  geri kalanı (`DatasetTable`, `PX`) UTC gösteriyor, aynı sayfada iki saat olmaz.
- Fail-open kuralı canlı yolun her katmanında: Redis yoksa DB yazımı sürer, WS bağlantısı
  kabul edilir ve `live.enabled=false` gönderilir, sayfa arşivden okumaya devam eder.
- `.env.example` bu oturumda izin ayarlarıyla korunuyor (okunamıyor da yazılamıyor da).
  Task 1 iki yeni anahtar için gereken satırları rapora yazar; dosyaya operatör ekler.
  `tests/unit/test_env_example.py::test_every_env_only_setting_is_documented` o satır
  eklenene kadar kırmızı kalır ve bu bilinerek bırakılır.

---

## Dosya yapısı

| Dosya | Sorumluluk |
| --- | --- |
| `src/yfin/stream/publish.py` | tick alan tablosu (tek kaynak), `channel()`, `tick_body()`, `TickPublisher` (tembel redis, fail-open, sayaç) |
| `src/yfin/stream/writer.py` | `_write_ticks` `accepted`'ı döndürür; `_write` commit sonrası yayınlar |
| `src/yfin/stream/runner.py` | `writer_config` iki yeni ayarı taşır |
| `src/yfin/core/config.py` | `yf_stream_publish_enabled` (DB), `yf_stream_publish_redis_url` (env-only) |
| `src/yfin/storage/settings_store.py` | `ENV_ONLY_FIELDS` + redis url |
| `src/yfin/core/metrics.py` | `yfin_stream_publish_total{result}` |
| `src/yfin/ui/live.py` | `/ui/ws`: origin kontrolü, `sub`/`unsub`, `snap`, pub/sub köprüsü, kuyruk, `dropped` |
| `src/yfin/ui/data.py` | `/ui/api/symbols/{s}/ticks`, `/ui/api/symbols/{s}/gaps` |
| `src/yfin/ui/__init__.py` | `install`: `live.router` kaydı |
| `scripts/dump_tick_fields.py` | alan tablosunu `web/src/live/tick-fields.json`'a yazar, `--check` |
| `web/src/live/{types.ts,socket.ts,store.ts,tick-fields.json}` | WS istemcisi, Zustand store, rAF coalescing |
| `web/src/panels/chart-data.ts` | saf dönüşümler: candles, volume, markers, gap bantları, canlı mum kovası |
| `web/src/panels/Chart.tsx` | lightweight-charts sarmalayıcı (iki pane, marker eklentisi, whitespace) |
| `web/src/panels/{GP,GIP,QR}.tsx` | paneller |
| `web/src/app/Shell.tsx` | `layout === "headed"` ise canlı şerit |
| `web/e2e/terminal.spec.ts` | tek Playwright senaryosu (CI'da koşmaz) |

---

### Task 1: Ayarlar, `publish.py` ve alan tablosu

**Files:** Create `src/yfin/stream/publish.py`, `scripts/dump_tick_fields.py`,
`tests/unit/test_stream_publish.py`; Modify `src/yfin/core/config.py`,
`src/yfin/storage/settings_store.py`, `src/yfin/core/metrics.py`, `pyproject.toml`.

- `yf_stream_publish_enabled: bool = False` (`_cfg("stream", ...)`, `yfin config set` ile açılır);
  `yf_stream_publish_redis_url: str = ""` (`_cfg` **yok** — kimlik bilgisi taşır, env-only,
  `ENV_ONLY_FIELDS`'e girer).
- `TICK_FIELDS`: `(wire_key, column, kind)` üçlülerinin tuple'ı; `kind ∈ {"str","int","ms","dec"}`.
  `s`, `t`, `p`, `mh` zorunlu; kalanı boşsa gönderilmez. `t` epoch ms.
- `tick_body(row)` bu tablodan üretilir; `channel(symbol) = f"yfin:tick:{symbol}"`.
- `TickPublisher(enabled, url)`: `publish(rows)` tek `pipeline()` içinde N `PUBLISH`;
  `yfin_stream_publish_total{result="ok"|"failed"|"disabled"}` her batch'te bir kez;
  bağlantı/publish hatası **durum değişiminde bir kez** loglanır.
- `redis` `api` extra'sından ana bağımlılığa taşınır (stream süreci `api` extra'sını kurmamış olabilir).
- `scripts/dump_tick_fields.py` tabloyu JSON'a yazar; `--check` CI kalıbı (`dump_openapi.py` gibi).
- Rapor: `.env.example`'a eklenecek iki satırın metni.

### Task 2: Writer commit sonrası yayınlar

**Files:** Modify `src/yfin/stream/writer.py`, `src/yfin/stream/runner.py`;
`tests/unit/test_stream_writer.py`.

- `_write_ticks` → `(written, unknown, accepted)`. `accepted` FK filtresinden geçen satırlar;
  `ON CONFLICT DO NOTHING` ile düşen tekrarlar da içinde (istemci `(s,t)` tekrarını atar).
- `_write`: `session.commit()` **başarılı olduktan sonra** `self._publisher.publish(accepted)`.
  Commit atarsa yayın yok — testte açıkça doğrulanır.
- `WriterConfig`: `publish_enabled`, `publish_redis_url`; `runner.writer_config` taşır.

### Task 3: `/ui/ws`

**Files:** Create `src/yfin/ui/live.py`, `tests/unit/test_ui_live.py`; Modify `src/yfin/ui/__init__.py`.

- Kimlik yok. `Origin`: `public_base_url` doluysa onunla, boşsa `Host` başlığıyla (şema
  bağımsız) eşleşmeli; yoksa/eşleşmiyorsa `4403`. `request_id` handler'da üretilir (WS
  `BaseHTTPMiddleware`'lerden geçmez).
- Çerçeveler: ↑ `sub`/`unsub`; ↓ `live`, `snap`, `tick`, `dropped`, `error{too_many|bad_symbol}`.
- `sub` sırası: Redis'e abone ol → `snap` gönder → tick'ler. Bağlantı başına 200 sembol.
- `snap` `live_quotes`'tan, `ticks` sorgusuyla aynı alan tablosu; senkron sorgular
  `run_in_threadpool`.
- `asyncio.Queue(maxsize=1000)`; dolarsa en eski düşer ve `dropped` sayacı artar.
- Redis'e ulaşılamıyorsa bağlantı kabul edilir, `live.enabled=false`, yalnız `snap` çalışır.
  `live.enabled` = API Redis'e ulaşıyor **ve** `yf_stream_publish_enabled`.

### Task 4: `/ui/api/symbols/{s}/ticks` ve `/gaps`

**Files:** Modify `src/yfin/ui/data.py`; `tests/unit/test_ui_data.py`,
`tests/repo/test_ui_live_repo.py`.

- `ticks?limit=` varsayılan 500, en çok 2000; `live_ticks`'ten en yeni önce, `snap` ile aynı gövde.
- `gaps?interval=&from=`: **yalnız açık** gap'ler (`resolved_at IS NULL`), pencereyle kesişenler,
  `gap_start_utc` artan. `bar_gaps` `/v1`'de yok ve `NEVER_EXPOSED`'da — bu yüzden UI rotası.

### Task 5: Tarayıcı canlı store

**Files:** Create `web/src/live/{types.ts,socket.ts,store.ts,tick-fields.json}` + testler;
Modify `web/package.json` (`zustand`).

- `socket.ts`: tek WS, 1 s→30 s üstel yeniden bağlanma, bağlanınca abonelik seti tekrar gönderilir.
- `store.ts`: `Map<symbol, Tick[]>` biriktirir, `requestAnimationFrame` her karede toplu uygular;
  sembol başına abone sayımı (sıfırda `unsub`); `QR` sembolü için 2000'lik halka, diğerleri son tick;
  `t < snap.t` olan tick atılır; `(s,t)` tekrarı atılır.
- vitest: rAF coalescing (sahte zamanlayıcı), referans sayımı, tekrar atma, `tick-fields.json`
  ile TS tipinin anahtarları.

### Task 6: Canlı şerit

**Files:** Modify `web/src/app/Shell.tsx`, `web/src/app/styles.css`; `Shell` testi.

- `spec.layout === "headed"` ise şerit son tick'i gösterir: fiyat, değişim, yüzde, seans
  durumu (`mh`), kopukluk kırmızı nokta. `live.enabled=false` → "canlı akış kapalı, son: HH:MM UTC"
  ve `snap` 30 s'de bir yinelenir. Arşivsiz sembol → "arşivlenmiyor".

### Task 7: Grafik altyapısı

**Files:** Create `web/src/panels/chart-data.ts`, `chart-data.test.ts`, `Chart.tsx`;
Modify `web/package.json` (`lightweight-charts`), `styles.css`.

- `chart-data.ts` (saf): `toCandles(rows)`, `toVolume(rows)`, `toMarkers(actions, candleTimes)`
  (aksiyon tarihini ≥ ilk mum zamanına yapıştırır, yoksa atar), `gapBands(gaps, interval, candleTimes)`
  (gap penceresini interval adımlarıyla whitespace zaman yuvalarına açar; toplam yuva sayısı
  sınırı aşarsa bant çizilmez ve panel not düşer), `applyTick(candles, tick, intervalMs)`.
- `Chart.tsx`: `createChart(el, {autoSize:true})`, ana pane candlestick, ikinci pane histogram
  hacim, `createSeriesMarkers` eklentisi, gap bandı için overlay histogram (`priceScaleId: ""`).
  Tema `styles.css`'teki değişkenlerden okunur (`getComputedStyle`), renkler tek yerde.

### Task 8: `GP`

**Files:** Create `web/src/panels/GP.tsx`, `GP.test.tsx`; Modify `panels/index.ts`, `api/client.ts`.

- Son 2 yıl `interval=1d`; hacim; `/actions`'tan temettü/split marker'ları; canlı tick son günü günceller.
- `client.ts`: `getBarsWindow(symbol, interval, fromISO, session?)` — satır sayısı yerine pencere.

### Task 9: `GIP`

**Files:** Create `web/src/panels/GIP.tsx`, `GIP.test.tsx`; Modify `panels/index.ts`.

- Arg `1m/5m/15m/60m`, varsayılan `5m`; son 5 işlem günü; `session=regular`; gap overlay;
  canlı mum: kova `floor(t / interval)` UTC, `mh` regular seans dışı tick atılır, her mum
  sınırında ve her yeniden bağlanmada son 2 bar REST'ten yenilenir.

### Task 10: `QR`

**Files:** Create `web/src/panels/QR.tsx`, `QR.test.tsx`; Modify `panels/index.ts`,
`web/package.json` (`@tanstack/react-virtual`).

- Açılışta `/ui/api/symbols/{s}/ticks?limit=500`, sonra canlı; sanallaştırılmış liste;
  kopuklukta ayırıcı satır; arşivsiz sembolde boş + açıklama.

### Task 11: Playwright ve ölçüm

**Files:** Create `web/e2e/terminal.spec.ts`, `web/playwright.config.ts`;
Modify `web/package.json`, `.gitignore`; `docs/measurements/websocket.md`.

- Tek senaryo: `AAPL GIP 5m` → mum görünür. CI'da koşmaz, yerel kabul ölçütü.
- Ölçüm: batch commit'ten tarayıcıda ekrana gecikme (p50/p99, tek sembol). Sonuç, yayının
  N `PUBLISH` mi sembol başına dizi mi olacağını belirler.

### Task 12: Spec, README, observability

**Files:** Modify `docs/superpowers/specs/2026-09-07-web-terminal-design.md`,
`docs/superpowers/specs/2026-09-07-observability-design.md`, `README.md`.

- Spec: 1c/1d satırları "yapıldı"; zaman diliminin UTC olduğu; `/gaps` ve `/ticks` sözleşmeleri.
- Observability: `yfin_stream_publish_total` satırı.
- README: `GP`, `GIP`, `QR` ve canlı yolun açılışı (`yf_stream_publish_enabled`, redis url).

---

## Uygulandı — 2026-09-08

Task 1-12 tamamlandı. Doğrulama: `ruff` ve `mypy` temiz (222 dosya),
`pytest tests/unit` 1961 geçti, `pytest -m repo tests/repo` 655 geçti,
`openapi.json` ve `tick-fields.json` güncel, `web`: tsc/eslint temiz,
vitest 204 geçti, `vite build` çalışıyor.

**Tek kırmızı:** `tests/unit/test_env_example.py::test_every_env_only_setting_is_documented`.
`.env.example` bu oturumda da izin ayarlarıyla korunuyor (ne okunabildi
ne yazılabildi), ve `yf_stream_publish_redis_url` env-only olduğu için
orada belgelenmek zorunda. Eklenecek satırlar:

```
# The live tick bus for the browser terminal. The switch that turns
# publishing on is yf_stream_publish_enabled in the settings table
# (`yfin config set`); this is only where to publish, and it is env-only
# because a Redis URL carries its password inside the string. Empty
# means publishing is off whatever the switch says.
YF_STREAM_PUBLISH_REDIS_URL=redis://localhost:6379/2
```

`yf_stream_publish_enabled` DB-managed olduğu için `.env.example`'a
girmesi zorunlu değil; test yalnız env-only alanları arıyor.

**Plandan sapmalar** (hepsi spec'in "Revizyonlar" bölümüne de yazıldı):

1. **`QR` sanallaştırılmadı.** `@tanstack/react-virtual` bağımlılığı
   eklendi ve geri alındı: mutlak konumlandırma terminalin tek inline
   stili olurdu. Tape zaten 2000 satırla sınırlı; panel en yeni 300'ü
   çiziyor, `.load-more` ile büyüyor.
2. **Günlük grafikte `BucketMode.Session`.** Günlük barın anı seans
   açılışıdır, UTC gece yarısı değil; 86.400'e yuvarlamak ikinci bir mum
   çizerdi. Canlı fiyat bugünün barını uzatır, yarınınkini uydurmaz.
3. **Ondalıklar `Decimal.normalize()` ile gönderiliyor.** `NUMERIC(28,12)`
   yüzünden her fiyat on iki sıfır taşıyordu; repo testi bunu yakaladı.
4. **Fiyatsız tick yayınlanmıyor** (`tick_body` `None` döner).
5. **`_write_ticks` `TickWrite` NamedTuple'ı döner**; sekiz repo testi
   `.written`'a taşındı.
6. **`live.connect_bus` ayrı bir fonksiyon**: repo testinin fakeredis'i
   yerine koyabildiği dikiş; abonelik sırası ve kanal adlandırması
   gerçek pub/sub'a karşı doğrulanıyor.
7. **`BUS_POLL_SECONDS` 200 ms.** `get_message` ve `subscribe` aynı
   bağlantıyı paylaşıyor, ikisi de tek kilit altında; kilidi bu kadar
   tutmak `sub` gecikmesini sınırlıyor. Ölçüm bu terimi ilk sıraya
   koyuyor (`docs/measurements/websocket.md`).

**Ölçüm bekliyor.** Commit → ekran gecikmesi ölçülmedi: açık bir hisse
piyasası gerekiyor (pre-market'te hisseler akmıyor, ölçülmüş).
`docs/measurements/websocket.md` yöntemi, dört sıçramayı ve sonucun neyi
karara bağladığını yazıyor.

**E2E koşulmadı.** Arşiv + `dist` + intraday barı olan bir sembol
istiyor; `web/playwright.config.ts` komutları taşıyor
(`npm run build`, API'yi kaldır, `npx playwright install chromium`,
`npm run e2e`).
