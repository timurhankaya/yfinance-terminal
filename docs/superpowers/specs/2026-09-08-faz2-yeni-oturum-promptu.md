# Faz 2 — yeni oturum promptu

Aşağıdaki iki bölümden **birini** olduğu gibi yeni bir oturuma
yapıştırın. İkisi paralel yürütülebilir (aralarındaki tek bağ, 2a-2'nin
2c-1'de tanımlanan grup paletini beklemesidir), ama tek oturumda tek
alt proje alın.

---

## A) 2c-1: görsel katman, primitifler ve sparkline

Bu repo (`yfinance`) bir yfinance veri ambarı, okuma API'si ve `/ui`
altında klavye odaklı bir web terminali. Faz 1 bitti: 61 dataset
terminalden okunabiliyor, canlı tick akışı ve grafikler çalışıyor.
Faz 2'nin görsel katmanı onaylı bir spec'e sahip:

**`docs/superpowers/specs/2026-09-08-web-terminal-faz2c-viz-design.md`**

Önce bu spec'i baştan sona oku. Bu oturumda **yalnız 2c-1**'i uygula:

1. `web/src/app/styles.css`: `--up`, `--down` ve `--group-a` … `--group-g`
   custom property'leri. Bugünkü çıplak hex'ler (`.strip-change.up`
   `#4cc38a`, `.down` `#ff6b6b`) bu değişkenlere çıkarılır.
2. `web/src/panels/viz/`: `scale.ts`, `colors.ts` (renkleri
   stylesheet'ten okur — kalıp `web/src/panels/Chart.tsx:36-49`),
   `Sparkline`, `Bars`, `Bullet`, `Treemap`, `Scatter` ve testleri.
   Renk SVG'ye `fill`/`stroke` özniteliğiyle verilir, `style` ile asla.
3. `src/yfin/ui/data.py`: `/ui/api/sparklines` rotası. Yanıt
   `Resource[SparklineSet]`; kaynak `price_history`
   (`symbol = ANY(...) AND session_date >= ...`); `symbols` ≤ 200,
   `points` 5–90 (varsayılan 30). `tests/unit` + `tests/repo`.
4. `web/src/api/client.ts`: `getSparklines(symbols, points)`.
5. Sparkline kolonu üç panelde üç yoldan: `WLA` bir `<td>` ekler, `EQS`
   kendi `Column[]` dizisine bir girdi, `DatasetTable`'a
   `extra?: Column<Row>[]` (aynı alan `Tab` ve `DatasetView`'dan
   geçirilir, `SCR` için).
6. `web/src/panels/viz/treemap.bench.ts` ve sonucun
   `docs/measurements/` altına yazılması.

`HEAT`, `COMP` ve mevcut panellerin grafikleri **bu oturumda yok**
(2c-2 ve 2c-3).

### Kurallar

- Spec bağlayıcıdır. Sapman gerekiyorsa önce söyle; gerekçe commit
  gövdesine yazılır.
- Legacy bırakma: deprecated alias, compat shim, çift yol yok.
- Ayırıcı her değer enum (TS string enum / Python `StrEnum`), çıplak
  dize yok.
- `web/src`'de `style=` yasak; sayılar sabit `en-US`, tarih-saatler UTC.
- Testi önce yaz.

### Doğrulama — hepsi yeşil olmalı

```
uv run ruff check . && uv run mypy src/yfin
uv run pytest -q tests/unit && uv run pytest -q -m repo tests/repo
uv run python scripts/dump_openapi.py --check
cd web && npm run check && npm run lint && npm test
```

Başlangıç durumu: unit 1990, repo 670, web 266, hepsi geçiyor.
Commit konusu İngilizce ve gövdesi "neden"i anlatır; **commit mesajına
e-posta adresi yazma** (hook reddediyor).

---

## B) 2a-1: yerleşim iskeleti

Bu repo (`yfinance`) bir yfinance veri ambarı, okuma API'si ve `/ui`
altında klavye odaklı bir web terminali. Bugün tek panelli. Faz 2'nin
yerleşim katmanı onaylı bir spec'e sahip:

**`docs/superpowers/specs/2026-09-08-web-terminal-faz2a-layout-design.md`**

Önce bu spec'i baştan sona oku. Bu oturumda **yalnız 2a-1**'i uygula:

1. `dockview` 8.2 kurulumu; `dockview.css` import'u
   (`web/src/app/main.tsx`); `--dv-*` değişkenlerinin `styles.css`
   token'larına eşlenmesi. Popout ve yüzen gruplar kapalı.
2. `web/src/app/Workspace.tsx`: dockview'i süren tek bileşen, jenerik
   panel renderer (`params` → `getPanel(code).component`).
   Bölünmemiş sayfa bugünkü DOM'u çizmeli.
3. Rotalar: `src/yfin/ui/pages.py`'ye `/ui/w/{path:path}` (catch-all
   **değil**), `web/src/app/App.tsx`'e `/ui/w/:name`.
4. `useGo`'nun panel bağlamına taşınması: bugün yedi panel
   (`EQS`, `WLA`, `FA`, `HOME`, `DS`, `curated`, `HELP`) tüm
   uygulamanın adresini değiştiriyor; odaklı paneli **yerinde**
   değiştirmeli.
5. `useListKeys`'in panel odağına bağlanması: bugün `window`'da
   dinliyor (`web/src/panels/common.tsx:237-261`), dört liste paneli
   açıkken `j` dördünü birden oynatır.
6. `Shell.test.tsx`'in yeniden yazımı; panel testleri değişmez.

Grup harfleri (2a-2) ve kayıtlı sayfalar (2a-3) **bu oturumda yok**.

Kurallar ve doğrulama komutları A bölümüyle aynıdır.
