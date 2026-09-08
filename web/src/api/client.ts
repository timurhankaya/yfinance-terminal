import type { Tick } from "../live/types";

// One fetch wrapper for the page. Same-origin and never cached: the API's
// Vary header names Authorization, which this page never sends, so a cached
// response could be served for far longer than the data behind it lives.

// The page reads through the terminal's own mirror of /v1: same routers,
// no OAuth2, a per-IP brake instead of plan metering. /v1 itself stays the
// contracted, metered API for Bearer clients.
export const DATA_BASE = "/ui/api/v1";

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
  detail?: string;
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    cache: "no-store",
  });
  if (!response.ok) {
    let problem: Problem = {};
    try {
      problem = (await response.json()) as Problem;
    } catch {
      // A non-JSON error body carries nothing worth showing.
    }
    // Title and detail together: the detail is where the API says what to
    // change (the filters it accepts, the page cap), and a panel that
    // shows only the title would drop the remedy.
    const title = problem.title ?? response.statusText;
    throw new ApiError(response.status, problem.type ?? "unknown", problem.detail ? `${title}: ${problem.detail}` : title);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
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

/** Symbols by ticker OR by company name.
 *
 *  `/ui/api/search`, not `/v1/symbols?q=`: the published route matches
 *  the symbol column and says so in its contract, which leaves a reader
 *  who knows "Akbank" but not `AKBNK.IS` with nothing. This is one of
 *  the terminal's own reads, like `news` and `sparklines`.
 *
 *  Not upper-cased on the way out: the route folds case itself, and the
 *  name half of the match is not a ticker. */
export async function searchSymbols(prefix: string): Promise<SymbolSummary[]> {
  const q = prefix.trim();
  if (q.length < SEARCH_MIN_PREFIX) return [];
  const page = await apiFetch<Page<SymbolSummary>>(`/ui/api/search?q=${encodeURIComponent(q)}`);
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

/** The wire types the catalogue reports for a column, verbatim. */
export enum WireType {
  Decimal = "string (decimal)",
  DateTime = "string (date-time)",
  Date = "string (date)",
  String = "string",
  Integer = "integer",
  Boolean = "boolean",
}

export interface CatalogColumn {
  name: string;
  type: WireType;
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

//: Rows per page for lists the user pages through with "Load more".
export const PAGE_SIZE = 200;

export interface RowPage {
  rows: Row[];
  next_cursor: string | null;
}

/** One page of a dataset. `cursor` continues the previous page of the
 *  same query; the caller appends and keeps `next_cursor`. */
export async function getDatasetPage(
  name: string, params: Record<string, string> = {}, cursor: string | null = null, limit: number = PAGE_SIZE,
): Promise<RowPage> {
  const search = new URLSearchParams({ ...params, limit: String(limit) });
  if (cursor) search.set("cursor", cursor);
  const page = await apiFetch<Page<Row>>(`${DATA_BASE}/datasets/${encodeURIComponent(name)}?${search}`);
  return { rows: page.data, next_cursor: page.next_cursor };
}

/** Dividends, splits and capital gains, oldest first as the API sends them. */
export function getActions(symbol: string): Promise<Rows> {
  const code = encodeURIComponent(symbol.trim().toUpperCase());
  return followPages(`${DATA_BASE}/symbols/${code}/actions?limit=${PAGE_LIMIT}`, MAX_PAGES);
}

/** The API's ReadableInterval, verbatim. The one vocabulary: the table
 *  below carries everything the page knows about each interval, so a new
 *  one is a single entry rather than four lists that can drift. */
export enum Interval {
  M1 = "1m",
  M5 = "5m",
  M15 = "15m",
  M60 = "60m",
  D1 = "1d",
  Wk1 = "1wk",
  Mo1 = "1mo",
}

export interface IntervalSpan {
  /** Calendar milliseconds one bar spans, generously: intraday bars only
   *  exist during sessions, so a day of 1m bars is ~390 bars in 24
   *  hours, and weekends hold none. */
  spanMs: number;
  /** Seconds one bar covers, which is what a chart's time axis wants. */
  seconds: number;
}

export const INTERVALS: Record<Interval, IntervalSpan> = {
  [Interval.M1]: { spanMs: 4 * 60_000, seconds: 60 },
  [Interval.M5]: { spanMs: 20 * 60_000, seconds: 300 },
  [Interval.M15]: { spanMs: 60 * 60_000, seconds: 900 },
  [Interval.M60]: { spanMs: 4 * 3_600_000, seconds: 3_600 },
  [Interval.D1]: { spanMs: 1.6 * 86_400_000, seconds: 86_400 },
  [Interval.Wk1]: { spanMs: 8 * 86_400_000, seconds: 604_800 },
  [Interval.Mo1]: { spanMs: 32 * 86_400_000, seconds: 2_592_000 },
};

/** Every interval, in the API's order. */
export const BAR_INTERVALS: Interval[] = Object.values(Interval);

const INTERVAL_NAMES: ReadonlySet<string> = new Set<string>(BAR_INTERVALS);

/** Whether a string off the wire or out of a URL names an interval. */
export function isInterval(value: string): value is Interval {
  return INTERVAL_NAMES.has(value);
}

/** The newest `rows` bars, oldest first as the API sends them. The API
 *  pages from the oldest bar, so the request starts far enough back for
 *  `rows` bars to fit and keeps the tail. */
export async function getBars(symbol: string, interval: Interval, rows: number, now = Date.now()): Promise<Row[]> {
  const code = encodeURIComponent(symbol.trim().toUpperCase());
  const from = new Date(now - INTERVALS[interval].spanMs * rows).toISOString();
  const search = new URLSearchParams({ interval, from, limit: String(PAGE_LIMIT) });
  const all = await followPages(`${DATA_BASE}/symbols/${code}/bars?${search}`, MAX_PAGES);
  return all.rows.slice(-rows);
}

//: The news route pages by size, not by cursor: 50 by default, 200 at most.
export const NEWS_PAGE = 50;
export const NEWS_MAX = 200;

export async function getNews(symbol: string, limit: number = NEWS_PAGE): Promise<NewsItem[]> {
  const code = encodeURIComponent(symbol.trim().toUpperCase());
  const page = await apiFetch<Page<NewsItem>>(`/ui/api/symbols/${code}/news?limit=${limit}`);
  return page.data;
}

// --- the charts and the tape ------------------------------------------------

/** Bars over a WINDOW rather than a row count.
 *
 *  `getBars` asks for "the newest N", which is what a table wants. A
 *  chart wants "the last two years" or "the last five sessions", and the
 *  difference matters at the edges: a row count over a thin symbol
 *  reaches back years, and over a busy one stops mid-session. */
export async function getBarsWindow(
  symbol: string,
  interval: Interval,
  fromISO: string,
  session?: string,
): Promise<Row[]> {
  const code = encodeURIComponent(symbol.trim().toUpperCase());
  const search = new URLSearchParams({ interval, from: fromISO, limit: String(PAGE_LIMIT) });
  // `session` is intraday-only; passing it above daily is a 422 rather
  // than a silent no-op, so the caller decides.
  if (session !== undefined) search.set("session", session);
  const all = await followPages(`${DATA_BASE}/symbols/${code}/bars?${search}`, MAX_PAGES);
  return all.rows;
}

/** ISO 8601 for a window that starts `days` ago. */
export function daysAgo(days: number, now = Date.now()): string {
  return new Date(now - days * 86_400_000).toISOString();
}

export interface GapRow {
  bar_interval: string;
  gap_start_utc: string;
  gap_end_utc: string;
  reason: string;
  detected_at: string;
}

/** Windows the archive knows it is missing, open ones only.
 *
 *  Not under `/ui/api/v1`: `bar_gaps` has no `/v1` route (it is in the
 *  contract's NEVER_EXPOSED list), so this is one of the terminal's own
 *  reads, like `news`. */
export async function getGaps(
  symbol: string,
  interval: Interval,
  fromISO: string,
): Promise<GapRow[]> {
  const code = encodeURIComponent(symbol.trim().toUpperCase());
  const search = new URLSearchParams({ interval, from: fromISO });
  const page = await apiFetch<Page<GapRow>>(`/ui/api/symbols/${code}/gaps?${search}`);
  return page.data;
}

//: What `QR` opens on before the socket takes over. The route's own
//: ceiling is 2000.
export const TICKS_DEFAULT = 500;

/** The newest ticks for one symbol, newest first, in the socket's own
 *  body shape -- so the opening page and the live rows render alike. */
export async function getTicks(symbol: string, limit = TICKS_DEFAULT): Promise<Tick[]> {
  const code = encodeURIComponent(symbol.trim().toUpperCase());
  const page = await apiFetch<Page<Tick>>(
    `/ui/api/symbols/${code}/ticks?limit=${String(limit)}`,
  );
  return page.data;
}

// --- sparklines -------------------------------------------------------------

export interface SparklineSeries {
  symbol: string;
  /** Closes, oldest first, as the API sends decimals: strings. */
  closes: string[];
  first_date: string;
  last_date: string;
}

export interface SparklineSet {
  points: number;
  series: SparklineSeries[];
  /** Symbols the archive has no bars for in the window. */
  missing: string[];
}

//: The route's own defaults and ceilings (`ui/data.py`). The symbol cap
//: is the socket's, since this is the other half of a watchlist row.
export const SPARKLINE_POINTS = 30;
export const SPARKLINE_MAX_SYMBOLS = 200;

/** The last `points` daily closes for a list of symbols, in ONE request.
 *
 *  One of the terminal's own reads, like `news` and `screens` -- not
 *  because `/v1` does not join this time, but because it does not batch:
 *  a 200-row watchlist through `/v1/symbols/{s}/bars` is 200 requests to
 *  draw 200 lines of thirty numbers. */
export async function getSparklines(
  symbols: string[],
  points: number = SPARKLINE_POINTS,
): Promise<SparklineSet> {
  const search = new URLSearchParams({ symbols: symbols.join(","), points: String(points) });
  const envelope = await apiFetch<{ data: SparklineSet }>(`/ui/api/sparklines?${search}`);
  return envelope.data;
}

// --- the screener -----------------------------------------------------------

export interface ScreenSummary {
  screen_key: string;
  title: string;
  description: string | null;
  kind: string;
  quote_type: string;
  sort_field: string;
  sort_asc: boolean;
  as_of_date: string | null;
  fetched_at: string | null;
  total: number | null;
  row_count: number | null;
}

/** A type alias rather than an interface, on purpose: `DataTable` is
 *  generic over `Record<string, unknown>`, and only a type alias gets the
 *  implicit index signature that satisfies it. */
export type ScreenRow = {
  rank_index: number;
  symbol: string;
  is_known: boolean;
  short_name: string | null;
  currency: string | null;
  exchange: string | null;
  market_state: string | null;
  price: string | null;
  change: string | null;
  change_percent: string | null;
  volume: number | null;
  market_cap: string | null;
  trailing_pe: string | null;
  fifty_two_week_change_percent: string | null;
};

export interface ScreenDetail {
  screen: ScreenSummary;
  rows: ScreenRow[];
  /** Where `rows` starts in the roster. */
  offset: number;
  /** True when there are more rows after this page. */
  truncated: boolean;
}

//: Rows per roster page, matching the route's own cap. A roster is
//: bigger than this by default: `yf_screen_size` 250 x
//: `yf_screen_max_pages` 4 is a thousand members.
export const SCREEN_PAGE = 250;

/** Every screen this deployment runs, with its latest run.
 *
 *  One of the terminal's own reads, like `news` and `gaps`: a screen is
 *  four tables, and `/v1` does not join. Reading it through the generic
 *  surface means four calls and a client-side join over the hundred-odd
 *  columns of `screen_quotes` to show twelve. */
export async function getScreens(): Promise<ScreenSummary[]> {
  const page = await apiFetch<Page<ScreenSummary>>("/ui/api/screens");
  return page.data;
}

/** One page of a screen's latest roster, in the screen's own order. */
export async function getScreen(key: string, offset = 0): Promise<ScreenDetail> {
  const code = encodeURIComponent(key.trim().toLowerCase());
  const search = new URLSearchParams({ limit: String(SCREEN_PAGE), offset: String(offset) });
  const envelope = await apiFetch<{ data: ScreenDetail }>(
    `/ui/api/screens/${code}?${search}`,
  );
  return envelope.data;
}
