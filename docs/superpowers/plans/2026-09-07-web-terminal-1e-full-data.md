# Web Terminal 1e: Tam Veri Kapsamı — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Yahoo'dan arşive alınan her veri kümesi (56 dataset + bars/actions/financials + `info`'nun 189 alanı) terminalde eksiksiz ve hatasız görünsün.

**Architecture:** Tek bir tipli tablo motoru (`DatasetTable`) katalogdaki kolon türlerinden hücre biçimini türetir; `DS` paneli kataloğun tamamını gezer ve herhangi bir dataset'i açar; aile bazlı kürate paneller (HDS, ERN, FUND, CAL, MKT, SCR, SRCH, DOM, REF) aynı motor üstünde sekme yapılandırmasıdır; `CA` ve `PX` `/symbols/{s}/actions` ve `/bars` uçlarını tablo olarak gösterir; DES `info`'nun her alanını bölümlere ayırıp listelenmemiş alanları "Other" altında toplar, hiçbir alan düşmez.

**Tech Stack:** React 19, TypeScript strict, vitest 4, mevcut `usePanelData`/`DataTable`/`useListKeys`; backend değişikliği yok (`/ui/api/v1` aynası her şeyi sunar).

**Spec:** `docs/superpowers/specs/2026-09-07-web-terminal-design.md` ("Tam veri kapsamı (1e)" bölümü)

## Global Constraints

- Inline `style=` yok (CSP `style-src 'self'`); tüm stil `styles.css`.
- `noUncheckedIndexedAccess` açık; `Record` erişimleri `?? null` ile.
- Komut grammar'ı değişmez: `[SYMBOL] [CODE] [ARGS...]`, arg token'ları ham gelir.
- Her panel `PanelSpec`; `registerAll` içine eklenir; HELP otomatik listeler.
- Veri hiçbir yerde sessizce düşmez: gizlenen tek kolon `raw_json`'dır ve satır detayında tam gösterilir.

---

### Task 1: Tipli tablo motoru ve istemci fonksiyonları

**Files:** Create `web/src/panels/table.tsx`, `web/src/panels/table.test.tsx`; Modify `web/src/api/client.ts`, `web/src/api/client.test.ts`.

- `client.ts`: `CatalogEntry`/`CatalogColumn` tipleri, `getCatalog()` (`/datasets`, süreç içi cache), `getDatasetRows(name, params, {pages=5, limit=1000})` cursor takipli, `getActions(symbol)`, `getBars(symbol, interval, limit)`; `getDataset` eski imzayı korur.
- `table.tsx`: `formatCell(value, wireType, name)`; `DatasetTable({ columns, rows, hidden? })`: `raw_json` sütunu gizli, j/k/Enter + tıklama satır detayı (tüm alanlar, `raw_json` pretty-print), URL değerleri link, decimal → `cell()` benzeri, date-time → yerel saat, integer → binlik ayraç, boolean → yes/no, null → "—". Symbol kolonunu sembol bağlamındaysa gizle (aynı değer her satırda).
- Testler: her wire türü için biçim; `raw_json` gizli ve detayda; Enter ile detay açılır.

### Task 2: `DS` katalog gezgini

**Files:** Create `web/src/panels/DS.tsx`, `web/src/panels/DS.test.tsx`; Modify `web/src/panels/index.ts`.

- `DS` (needsSymbol=false): argsız → katalog aileye göre gruplu liste (ad, kapsam, açıklama, filtreler); Enter/tıklama → `DS <ad>`.
- `DS <ad> [k=v ...]`: katalogdan girdi, `symbol_scoped` ise bağlam sembolünü gönderir (yoksa "Type a symbol" kartı; `symbol_optional` olanlar sembolsüz de çalışır), `k=v` token'ları filtre; bilinmeyen anahtar → usage hatası (katalog filtre listesi). 422/404 problem başlığı gösterilir.

### Task 3: Kürate sekmeli paneller

**Files:** Create `web/src/panels/curated.tsx`, `web/src/panels/curated.test.tsx`; Modify `index.ts`.

- `tabbedPanel({code, title, tabs:[{key,label,dataset,symbol:"required"|"optional"|"none", params?}]})` → `PanelSpec`; args `{tab, ...k=v}`; sekme URL'de.
- Paneller: HDS (major_holders, institutional_holders, mutualfund_holders, insider_roster_holders, insider_transactions, insider_purchases), ERN (earnings_dates, earnings_history, earnings_estimate, revenue_estimate, eps_trend, eps_revisions, growth_estimates, ticker_calendar, ticker_calendar_history), FUND (fund_profile, fund_top_holdings, fund_weightings, fund_metrics), CAL (earnings_calendar, economic_calendar, ipo_calendar, splits_calendar — none), MKT (market_status, market_summary, market_status_history, market_summary_history — none), SCR (screens, screen_runs, screen_members — none; screen_quotes — required), SRCH (search_quotes, search_lists, search_report_hits, lookup_results, lookup_totals — none), DOM (domains, domain_metrics, domain_top_companies, domain_top_funds, domain_top_movers, domain_report_links, research_reports — none), REF (fast_info, fast_info_history, history_metadata, info_history, shares_full, company_officers, news_symbols — required).

### Task 4: `CA` ve `PX`

**Files:** Create `web/src/panels/CA.tsx`, `web/src/panels/PX.tsx` (+ testler); Modify `index.ts`.

- `CA`: tüm sayfalar, en yeni önce, tür/tarih/değer; boş → EmptyCard.
- `PX [interval=1d] [limit=200]`: `/bars` son N bar (API eskiden yeniye verir; UI ters çevirir), OHLC/adj/volume; geçersiz interval → usage.

### Task 5: DES tam `info`

**Files:** Modify `web/src/panels/DES.tsx`, `web/src/panels/DES.test.tsx`.

- `SECTIONS`: Identity, Contact, Business summary, Price & volume, Valuation, Income & cash, Balance sheet, Margins & growth, Share statistics, Dividends & splits, Analyst view, Key dates, Fund, Crypto; listelenmeyen her anahtar "Other" bölümünde; null alanlar sayılır ve "n empty fields not shown" notu.
- Biçim: açık `PERCENT_FRACTION` / `PERCENT_ALREADY` kümeleri; `*_date`/`*_timestamp*`/`*_epoch_date`/`*_time` epoch → tarih; büyük sayılar K/M/B/T; link alanları.
- Alt kısımda `company_officers` tablosu.
- Test: sahte info'da bilinmeyen anahtar "Other"da; yüzde ve epoch biçimi; hiçbir non-null anahtar düşmez (render edilen dt sayısı = non-null anahtar sayısı).

### Task 6: HELP/usage, stil, spec, README, tarayıcı doğrulaması

- `PanelSpec.usage?: string`; HELP her panelin usage'ını gösterir.
- `styles.css`: detay paneli, geniş tablo `overflow-x`, sekme satırı sarması.
- Spec "Tam veri kapsamı (1e)" bölümü ve panel tablosu; README panel listesi.
- Playwright: DS katalog, DS bir dataset, HDS/ERN/CAL/MKT/DOM/REF sekmeleri, CA, PX, DES tam.
