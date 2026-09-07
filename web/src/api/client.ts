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
