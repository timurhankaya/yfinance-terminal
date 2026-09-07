// Links a row implies but does not carry: a research report is a page on
// Yahoo keyed by report_id, a sector has a page keyed by its domain_key,
// any symbol has a quote page. The URL patterns were checked against the
// live site on 2026-09-07; a pattern Yahoo retires turns into a dead
// link, not a wrong number.
import type { Row } from "../api/client";

export interface LinkRule {
  label: string;
  href: (row: Row) => string | null;
}

const YF = "https://finance.yahoo.com";

function str(row: Row, key: string): string | null {
  const v = row[key];
  return typeof v === "string" && v !== "" ? v : null;
}

function enc(value: string): string {
  return encodeURIComponent(value);
}

const REPORT: LinkRule = {
  label: "report",
  href: (row) => {
    const id = str(row, "report_id");
    return id ? `${YF}/research/reports/${enc(id)}` : null;
  },
};

const QUOTE: LinkRule = {
  label: "quote",
  href: (row) => {
    const symbol = str(row, "symbol");
    return symbol ? `${YF}/quote/${enc(symbol)}/` : null;
  },
};

const DOMAIN: LinkRule = {
  label: "sector page",
  href: (row) => {
    const key = str(row, "domain_key");
    if (!key) return null;
    const parent = str(row, "parent_key");
    // An industry lives under its sector; a sector stands alone. When
    // the row does not say (domain_* tables carry the key only), the
    // sector form is the one Yahoo redirects correctly for both.
    return parent ? `${YF}/sectors/${enc(parent)}/${enc(key)}/` : `${YF}/sectors/${enc(key)}/`;
  },
};

const SCREEN: LinkRule = {
  label: "screener",
  href: (row) => {
    const key = str(row, "screen_key");
    return key ? `${YF}/research-hub/screener/${enc(key)}/` : null;
  },
};

const HOLDING: LinkRule = {
  label: "holding quote",
  href: (row) => {
    const symbol = str(row, "holding_symbol");
    return symbol ? `${YF}/quote/${enc(symbol)}/` : null;
  },
};

//: Per dataset. Datasets with a symbol column get QUOTE as well (see
//: `linksFor`), so only the extra rules are listed here.
const BY_DATASET: Record<string, LinkRule[]> = {
  research_reports: [REPORT],
  domain_report_links: [REPORT, DOMAIN],
  search_report_hits: [REPORT],
  domains: [DOMAIN],
  domain_metrics: [DOMAIN],
  domain_top_companies: [DOMAIN],
  domain_top_funds: [DOMAIN],
  domain_top_movers: [DOMAIN],
  screens: [SCREEN],
  screen_runs: [SCREEN],
  screen_members: [SCREEN],
  fund_top_holdings: [HOLDING],
};

/** The link rules for one dataset, given its column names. */
export function linksFor(dataset: string, columns: string[]): LinkRule[] {
  const rules = [...(BY_DATASET[dataset] ?? [])];
  if (columns.includes("symbol")) rules.push(QUOTE);
  return rules;
}

/** The symbol's own quote page, for DES. */
export function quoteUrl(symbol: string): string {
  return `${YF}/quote/${enc(symbol)}/`;
}
