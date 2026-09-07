// One fetch wrapper for the page. Same-origin cookie, never cached: the
// API's Vary header names Authorization, not Cookie, so a cached /v1
// response could outlive a logout.

// The page reads through the terminal's own mirror of /v1: same routers,
// no OAuth2, a per-IP brake instead of plan metering. /v1 itself stays the
// contracted, metered API for Bearer clients.
export const DATA_BASE = "/ui/api/v1";

export class UnauthorizedError extends Error {
  constructor() {
    super("unauthorized");
    this.name = "UnauthorizedError";
  }
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly type: string,
    title: string,
  ) {
    super(title);
    this.name = "ApiError";
  }
}

interface Problem {
  type?: string;
  title?: string;
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    cache: "no-store",
  });
  if (response.status === 401) throw new UnauthorizedError();
  if (!response.ok) {
    let problem: Problem = {};
    try {
      problem = (await response.json()) as Problem;
    } catch {
      // A non-JSON error body carries nothing worth showing.
    }
    throw new ApiError(response.status, problem.type ?? "unknown", problem.title ?? response.statusText);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export interface Me {
  authenticated: boolean;
  expires_at: number | null;
  live_enabled: boolean;
  /** No login on this terminal; the page never shows the modal. */
  public: boolean;
}

export interface SymbolDetail {
  symbol: string;
  short_name: string | null;
  long_name: string | null;
  exchange: string | null;
  full_exchange_name: string | null;
  currency: string | null;
  quote_type: string | null;
  timezone: string | null;
  is_active: boolean;
  info: Record<string, unknown> | null;
}

export function getMe(): Promise<Me> {
  return apiFetch<Me>("/ui/api/me");
}

export async function login(password: string): Promise<void> {
  const body = new URLSearchParams({ password });
  await apiFetch<void>("/ui/api/login", { method: "POST", body });
}

export async function logout(): Promise<void> {
  await apiFetch<void>("/ui/api/logout", { method: "POST" });
}

export async function getSymbol(symbol: string): Promise<SymbolDetail> {
  const code = encodeURIComponent(symbol.trim().toUpperCase());
  const envelope = await apiFetch<{ data: SymbolDetail }>(`${DATA_BASE}/symbols/${code}`);
  return envelope.data;
}

export interface SymbolSummary {
  symbol: string;
  short_name: string | null;
  long_name: string | null;
  exchange: string | null;
  quote_type: string | null;
}

export interface FinancialFact {
  period_end: string;
  item_key: string;
  value: string;
  currency: string | null;
}

export interface NewsItem {
  news_id: string;
  title: string;
  summary: string | null;
  pub_date: string;
  provider_name: string | null;
  link: string | null;
  thumbnail_url: string | null;
}

interface Page<T> {
  data: T[];
  next_cursor: string | null;
}

export const SEARCH_MIN_PREFIX = 2;

export async function searchSymbols(prefix: string): Promise<SymbolSummary[]> {
  const q = prefix.trim().toUpperCase();
  if (q.length < SEARCH_MIN_PREFIX) return [];
  const page = await apiFetch<Page<SymbolSummary>>(`${DATA_BASE}/symbols?q=${encodeURIComponent(q)}&limit=20`);
  return page.data;
}

const FINANCIALS_PAGE = 1000;
// Safety bound only: 5 pages of 1000 rows (5000 rows) is far above any real
// statement's row count. A page beyond this cap is silently not fetched.
const FINANCIALS_MAX_PAGES = 5;

export async function getFinancials(symbol: string, statement: string, freq: string): Promise<FinancialFact[]> {
  const code = encodeURIComponent(symbol.trim().toUpperCase());
  const base = `${DATA_BASE}/symbols/${code}/financials?statement=${encodeURIComponent(statement)}&freq=${encodeURIComponent(freq)}&limit=${FINANCIALS_PAGE}`;
  const rows: FinancialFact[] = [];
  let cursor: string | null = null;
  for (let i = 0; i < FINANCIALS_MAX_PAGES; i += 1) {
    const url: string = cursor ? `${base}&cursor=${encodeURIComponent(cursor)}` : base;
    const page: Page<FinancialFact> = await apiFetch<Page<FinancialFact>>(url);
    rows.push(...page.data);
    cursor = page.next_cursor;
    if (!cursor) break;
  }
  return rows;
}

export type Row = Record<string, unknown>;

export async function getDataset(name: string, symbol: string, params: Record<string, string> = {}): Promise<Row[]> {
  const search = new URLSearchParams({ ...params, symbol: symbol.trim().toUpperCase(), limit: "200" });
  const page = await apiFetch<Page<Row>>(`${DATA_BASE}/datasets/${encodeURIComponent(name)}?${search}`);
  return page.data;
}

// --- the catalogue and any dataset ------------------------------------------

export interface CatalogColumn {
  name: string;
  /** The wire type: `string (decimal)`, `string (date-time)`, `string (date)`, `string`, `integer`, `boolean`. */
  type: string;
  nullable: boolean;
}

export interface CatalogEntry {
  name: string;
  family: string;
  scope: string;
  kind: string;
  table: string;
  sort_key: string[];
  descending: boolean;
  filters: string[];
  symbol_scoped: boolean;
  description: string;
  columns: CatalogColumn[];
}

let catalogCache: Promise<CatalogEntry[]> | null = null;

/** The catalogue, fetched once per page load. A failure is not cached, so
 *  a retry after a network blip asks again. */
export function getCatalog(): Promise<CatalogEntry[]> {
  if (catalogCache === null) {
    catalogCache = apiFetch<Page<CatalogEntry>>(`${DATA_BASE}/datasets`)
      .then((page) => page.data)
      .catch((err: unknown) => {
        catalogCache = null;
        throw err;
      });
  }
  return catalogCache;
}

/** Tests only: forget the cached catalogue. */
export function resetCatalogCache(): void {
  catalogCache = null;
}

//: The UI principal's page cap; asking for more is a 422.
export const PAGE_LIMIT = 1000;
//: Pages followed per request. 5000 rows is more than any panel can show
//: usefully; beyond this the panel says the list was cut.
export const MAX_PAGES = 5;

export interface Rows {
  rows: Row[];
  /** True when MAX_PAGES was reached with a cursor still to follow. */
  truncated: boolean;
}

async function followPages(base: string, pages: number): Promise<Rows> {
  const rows: Row[] = [];
  let cursor: string | null = null;
  for (let i = 0; i < pages; i += 1) {
    const url: string = cursor ? `${base}&cursor=${encodeURIComponent(cursor)}` : base;
    const page: Page<Row> = await apiFetch<Page<Row>>(url);
    rows.push(...page.data);
    cursor = page.next_cursor;
    if (!cursor) return { rows, truncated: false };
  }
  return { rows, truncated: true };
}

/** Every row of one dataset, following cursors up to `pages` pages.
 *  `params` are the dataset's filters plus, for a symbol-scoped dataset,
 *  `symbol`; the caller decides, because the catalogue says which is which. */
export function getDatasetRows(
  name: string, params: Record<string, string> = {}, pages: number = MAX_PAGES,
): Promise<Rows> {
  const search = new URLSearchParams({ ...params, limit: String(PAGE_LIMIT) });
  return followPages(`${DATA_BASE}/datasets/${encodeURIComponent(name)}?${search}`, pages);
}

/** Dividends, splits and capital gains, oldest first as the API sends them. */
export function getActions(symbol: string): Promise<Rows> {
  const code = encodeURIComponent(symbol.trim().toUpperCase());
  return followPages(`${DATA_BASE}/symbols/${code}/actions?limit=${PAGE_LIMIT}`, MAX_PAGES);
}

//: The API's ReadableInterval, verbatim.
export const BAR_INTERVALS = ["1m", "5m", "15m", "60m", "1d", "1wk", "1mo"];

//: Calendar milliseconds one bar of each interval spans, generously:
//: intraday bars only exist during sessions, so a day of 1m bars is ~390
//: bars in 24 hours, and weekends hold none.
const BAR_SPAN_MS: Record<string, number> = {
  "1m": 4 * 60_000,
  "5m": 20 * 60_000,
  "15m": 60 * 60_000,
  "60m": 4 * 3_600_000,
  "1d": 1.6 * 86_400_000,
  "1wk": 8 * 86_400_000,
  "1mo": 32 * 86_400_000,
};

/** The newest `rows` bars, oldest first as the API sends them. The API
 *  pages from the oldest bar, so the request starts far enough back for
 *  `rows` bars to fit and keeps the tail. */
export async function getBars(symbol: string, interval: string, rows: number, now = Date.now()): Promise<Row[]> {
  const code = encodeURIComponent(symbol.trim().toUpperCase());
  const span = BAR_SPAN_MS[interval] ?? BAR_SPAN_MS["1d"]!;
  const from = new Date(now - span * rows).toISOString();
  const search = new URLSearchParams({ interval, from, limit: String(PAGE_LIMIT) });
  const all = await followPages(`${DATA_BASE}/symbols/${code}/bars?${search}`, MAX_PAGES);
  return all.rows.slice(-rows);
}

export async function getNews(symbol: string): Promise<NewsItem[]> {
  const code = encodeURIComponent(symbol.trim().toUpperCase());
  const page = await apiFetch<Page<NewsItem>>(`/ui/api/symbols/${code}/news`);
  return page.data;
}
