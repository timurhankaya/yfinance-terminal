# Terminal UI polish

User direction: implement directly; a more vivid Bloomberg-inspired terminal, visible amber/blue/violet selection, white aligned GitHub icon, aligned tables, descriptive tab tooltips, production verification.

Architecture: keep React/Dockview navigation and data contracts. Use shared CSS tokens and a single accessible tooltip layer for annotated controls, rendered outside scrolling panels. Preserve existing accent storage and semantic gain/loss colours.

- [x] Reproduce layout at desktop and narrow widths; add browser checks for usable accent buttons and hover/focus tooltips.
- [x] Update Shell.tsx and styles.css: labelled nonshrinking accent controls, local white GitHub SVG, compact responsive command/footer layout.
- [x] Add TooltipLayer.tsx and tab-help.ts; annotate market/symbol/dock and dataset/statement/snapshot tabs. Escape, focus/hover, viewport bounds, scroll dismissal and ARIA descriptions must work.
- [x] Refine shared table headers, number alignment, rows and panel geometry. Scope responsive grids to the dock panel width.
- [x] Run check, lint, unit suite, build and browser tests against the local API; inspect screenshots at desktop, narrow and split widths. Record real limitations.


## Verification and review notes

- TypeScript check, ESLint and production build pass.
- 555 unit tests pass: `NODE_OPTIONS=--no-experimental-webstorage npm --prefix web test`. The flag prevents the runtime's experimental Web Storage global from masking jsdom storage.
- Browser preview: `VITE_API_ORIGIN=http://localhost:8099 npx vite preview --host 127.0.0.1 --port 5174` from `web`.
- Browser suite: `E2E_BASE_URL=http://127.0.0.1:5174 E2E_ENFORCE_CSP=1 E2E_NOW=2026-08-15T12:00:00Z npm --prefix web run e2e`. The explicit test clock uses the local archive's August intraday bars; the real current nine-day window contains no AAPL 5m rows. No production clock or data was changed.
- The UI polish browser suite injects the production CSP and checks for violations, in addition to geometry, readable labels, accent persistence, hover/focus help and Escape dismissal.
- Visual review: 1440×1000 desktop, 390×844 narrow, and two 711px symbol panels. Each split panel's scroll width equals its client width.
- Existing production bundle warning remains: approximately 1 MB JavaScript before gzip (287 kB gzip). No new dependencies.
- Concurrent README, source-terms documentation, footer wording and associated test edits were preserved.

Final browser result: **15 passed** (20.4s); all seven UI polish checks ran with the production CSP.

## Search and symbol detail follow-up

- Added a visible Search action and a responsive discovery dialog with aligned symbol, company and exchange columns, loading/empty/error states, retry, stale-result protection, keyboard focus trapping and focus restoration.
- Refined security identity, summary statistics, chart/range card layout, company profile and GP/FA/HDS/N quick links using the shared terminal palette and panel-width breakpoints.
- Rebuilt the stale local API image to expose its existing search route, then started it with `YFAPI_UI_ENABLED=true docker compose up -d --no-deps api`. Verified real Apple search results and production UI at port 8099; no backend source or data changes were needed.
- TypeScript, lint and production build pass. Final unit suite: **559 passed** across 48 files with the Web Storage flag above.
- Final browser suite: **18 passed**, using `E2E_BASE_URL=http://127.0.0.1:8099 E2E_ENFORCE_CSP=1 E2E_NOW=2026-08-15T12:00:00Z npm --prefix web run e2e`. Includes search-to-detail navigation at 390px and 1440px, financial quick links, overflow checks, focus trapping and Escape restoration.
- Reviewed desktop/mobile search and security overview screenshots. Existing bundle warning and concurrent edits noted above remain unchanged.

## Filters, pagination, search identity and normal page navigation (2026-09-15)

- Server filters use a labelled, collapsible form with explicit Apply/Clear actions; loaded-row filters remain separate and show their scope. Empty/error results keep recovery controls. Empty parameters are removed from commands.
- Rows per load and Load more share a responsive sticky table footer. EQS appends roster pages instead of replacing them, supports local symbol/name/exchange filtering, and keeps page size in its URL. Pagination guards duplicate clicks, stale responses and an empty terminal page.
- Search picks carry their kind. Reproduced PG being interpreted as the saved-pages function when selecting Procter & Gamble; security picks now navigate as securities. Single-letter tickers resolve by the exact symbol endpoint.
- Number/text controls use unique IDs across panels. Enter commits without leaking into row-opening shortcuts. Activated tooltips dismiss so they cannot cover the next tab.
- Latest user direction supersedes the old always-Dockview page and Ctrl+Enter entry behavior: address routes render a normal SinglePanel. Only + enters a workspace; Ctrl+Enter remains a split shortcut after entering a workspace. Browser Back from + returns to the original single page.
- Symbol/function routes canonicalize case and explicit arguments without extra history entries. `/ui/t/akbnk.is` resolves to `/ui/t/AKBNK.IS/DES`; tabs and filters update the address and survive reload/back/forward.
- Concurrent financial-chart, empty-state, saved-page and documentation edits were retained. Final browser verification uses the real API/UI at 8099: Vite preview serves `/ui/`, while the production server supports both `/ui` and `/ui/`.

Final verification for this follow-up: **573 unit tests passed** (48 files), **32 Chromium tests passed** against the rebuilt API/UI at `http://127.0.0.1:8099`, with production CSP checks enabled. TypeScript, ESLint, production build and `git diff --check` passed. Browser coverage includes 390–3440px layouts, search identity collisions, single-letter tickers, empty-filter recovery, append pagination, page-size reload, canonical symbol links, tab history and explicit + workspace entry/Back exit. The pre-existing bundle-size warning remains (~291 kB JavaScript gzip). Local Docker API was rebuilt with UI enabled; no remote deployment or commit was performed.
