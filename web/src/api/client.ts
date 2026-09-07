// One fetch wrapper for the page. Same-origin cookie, never cached: the
// API's Vary header names Authorization, not Cookie, so a cached /v1
// response could outlive a logout.

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
  const envelope = await apiFetch<{ data: SymbolDetail }>(`/v1/symbols/${code}`);
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
  const page = await apiFetch<Page<SymbolSummary>>(`/v1/symbols?q=${encodeURIComponent(q)}&limit=20`);
  return page.data;
}

const FINANCIALS_PAGE = 1000;
// Safety bound only: 5 pages of 1000 rows (5000 rows) is far above any real
// statement's row count. A page beyond this cap is silently not fetched.
const FINANCIALS_MAX_PAGES = 5;

export async function getFinancials(symbol: string, statement: string, freq: string): Promise<FinancialFact[]> {
  const code = encodeURIComponent(symbol.trim().toUpperCase());
  const base = `/v1/symbols/${code}/financials?statement=${statement}&freq=${freq}&limit=${FINANCIALS_PAGE}`;
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

export async function getDataset(
  name: string, symbol: string, params: Record<string, string> = {},
): Promise<Record<string, unknown>[]> {
  const search = new URLSearchParams({ ...params, symbol: symbol.trim().toUpperCase(), limit: "200" });
  const page = await apiFetch<Page<Record<string, unknown>>>(`/v1/datasets/${name}?${search}`);
  return page.data;
}

export async function getNews(symbol: string): Promise<NewsItem[]> {
  const code = encodeURIComponent(symbol.trim().toUpperCase());
  const page = await apiFetch<Page<NewsItem>>(`/ui/api/symbols/${code}/news`);
  return page.data;
}
