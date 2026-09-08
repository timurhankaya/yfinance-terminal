// The curated panels: one mnemonic per data family, a tab per dataset,
// all rendered through DatasetView. Each is configuration, not code, so
// adding a dataset to the terminal is one line here (and DS reaches it
// even before that).
import { useGo } from "../commands/go";
import { Layout, type PanelArgs, type PanelProps, type PanelSpec } from "../commands/types";
import { DatasetView, ExtraColumn, SymbolMode, filtersOf, parseFilters } from "./dataset";

/** How a tab takes the strip's symbol. `Required`: the tab needs it and
 *  says so without one; `Auto`/`None` as in DatasetView. */
export enum TabSymbol {
  Required = "required",
  Auto = "auto",
  None = "none",
}

/** A required tab still asks the API scoped to the symbol; it only refuses
 *  to render without one. */
const MODE_OF: Record<TabSymbol, SymbolMode> = {
  [TabSymbol.Required]: SymbolMode.Auto,
  [TabSymbol.Auto]: SymbolMode.Auto,
  [TabSymbol.None]: SymbolMode.None,
};

export interface Tab {
  key: string;
  label: string;
  dataset: string;
  symbol: TabSymbol;
  /** Columns beyond the catalogue's, declared here and built by the
   *  view. Configuration, like the rest of a tab. */
  extra?: ExtraColumn[];
}

export interface TabbedPanelSpec {
  code: string;
  title: string;
  tabs: Tab[];
}

function usageOf(spec: TabbedPanelSpec): string {
  return `${spec.code} [${spec.tabs.map((t) => t.key).join("|")}] [filter=value ...]`;
}

function firstTab(spec: TabbedPanelSpec): Tab {
  const first = spec.tabs[0];
  if (first === undefined) throw new Error(`${spec.code}: a tabbed panel needs at least one tab`);
  return first;
}

export function tabbedPanel(spec: TabbedPanelSpec): PanelSpec {
  const usage = `Usage: ${usageOf(spec)}`;
  const keys = spec.tabs.map((t) => t.key);
  const first = firstTab(spec);

  function parseArgs(tokens: string[]): PanelArgs {
    const [head, ...rest] = tokens;
    if (head === undefined) return { tab: first.key };
    const key = head.toLowerCase();
    if (keys.includes(key)) return { tab: key, ...parseFilters(rest, usage) };
    if (head.includes("=")) return { tab: first.key, ...parseFilters(tokens, usage) };
    throw new Error(usage);
  }

  function Component({ symbol, args }: PanelProps) {
    const go = useGo();
    const current = spec.tabs.find((t) => t.key === args.tab) ?? first;
    const filters = filtersOf(args, ["tab"]);
    const needs = current.symbol === TabSymbol.Required && symbol === null;
    return (
      <section>
        <div className="tabs" role="tablist" aria-label={spec.title}>
          {spec.tabs.map((tab) => (
            <button
              key={tab.key}
              role="tab"
              aria-selected={tab.key === current.key}
              className={tab.key === current.key ? "tab tab-active" : "tab"}
              onClick={() => go(({ symbol, code: spec.code, args: { ...filters, tab: tab.key } }))}
            >
              {tab.label}
            </button>
          ))}
        </div>
        {needs ? (
          <p className="muted">
            {current.label} is per symbol: type one first, e.g. AAPL {spec.code} {current.key}.
          </p>
        ) : (
          <DatasetView
            name={current.dataset}
            symbol={symbol}
            filters={filters}
            mode={MODE_OF[current.symbol]}
            extra={current.extra}
          />
        )}
      </section>
    );
  }
  Component.displayName = spec.code;

  return {
    code: spec.code,
    title: spec.title,
    usage: usageOf(spec),
    // A panel whose every tab is market-wide or optional runs without a
    // symbol; one with a required tab still opens (on its first tab) so
    // the market-wide tabs stay reachable, and the required tab says
    // what it needs.
    needsSymbol: spec.tabs.every((t) => t.symbol === TabSymbol.Required),
    layout: Layout.Single,
    parseArgs,
    component: Component,
  };
}

const sym = (key: string, label: string, dataset: string, extra?: ExtraColumn[]): Tab => ({ key, label, dataset, symbol: TabSymbol.Required, extra });
const mkt = (key: string, label: string, dataset: string, extra?: ExtraColumn[]): Tab => ({ key, label, dataset, symbol: TabSymbol.None, extra });
const opt = (key: string, label: string, dataset: string, extra?: ExtraColumn[]): Tab => ({ key, label, dataset, symbol: TabSymbol.Auto, extra });

export const HDS_PANEL = tabbedPanel({
  code: "HDS",
  title: "Holders: major, institutional, funds, insiders",
  tabs: [
    sym("major", "Major", "major_holders"),
    sym("inst", "Institutions", "institutional_holders"),
    sym("funds", "Mutual funds", "mutualfund_holders"),
    sym("roster", "Insider roster", "insider_roster_holders"),
    sym("trades", "Insider transactions", "insider_transactions"),
    sym("activity", "Insider activity", "insider_purchases"),
  ],
});

export const ERN_PANEL = tabbedPanel({
  code: "ERN",
  title: "Earnings: dates, history, estimates, revisions, calendar",
  tabs: [
    sym("dates", "Dates", "earnings_dates"),
    sym("history", "History", "earnings_history"),
    sym("eps", "EPS estimate", "earnings_estimate"),
    sym("revenue", "Revenue estimate", "revenue_estimate"),
    sym("trend", "EPS trend", "eps_trend"),
    sym("revisions", "EPS revisions", "eps_revisions"),
    sym("growth", "Growth estimates", "growth_estimates"),
    sym("calendar", "Next dates", "ticker_calendar"),
    sym("calhist", "Next dates history", "ticker_calendar_history"),
  ],
});

export const FUND_PANEL = tabbedPanel({
  code: "FUND",
  title: "Fund profile: family, holdings, weightings, metrics (ETFs and mutual funds)",
  tabs: [
    sym("profile", "Profile", "fund_profile"),
    sym("holdings", "Top holdings", "fund_top_holdings"),
    sym("weights", "Weightings", "fund_weightings"),
    sym("metrics", "Metrics", "fund_metrics"),
  ],
});

export const CAL_PANEL = tabbedPanel({
  code: "CAL",
  title: "Market calendars: earnings, economic, IPO, splits",
  tabs: [
    opt("earnings", "Earnings", "earnings_calendar"),
    mkt("economic", "Economic", "economic_calendar"),
    opt("ipo", "IPO", "ipo_calendar"),
    opt("splits", "Splits", "splits_calendar"),
  ],
});

export const MKT_PANEL = tabbedPanel({
  code: "MKT",
  title: "Markets: status and headline indices, with history",
  tabs: [
    mkt("status", "Status", "market_status"),
    opt("summary", "Summary", "market_summary"),
    mkt("statushist", "Status history", "market_status_history"),
    sym("summaryhist", "Summary history", "market_summary_history"),
  ],
});

export const SCR_PANEL = tabbedPanel({
  code: "SCR",
  title: "Screens: definitions, runs, members, quote snapshots",
  tabs: [
    mkt("list", "Screens", "screens"),
    mkt("runs", "Runs", "screen_runs"),
    // The one curated tab that is a roster of symbols, which is what a
    // sparkline column is for.
    opt("members", "Members", "screen_members", [ExtraColumn.Sparkline]),
    sym("quotes", "Quote snapshot", "screen_quotes"),
  ],
});

export const SRCH_PANEL = tabbedPanel({
  code: "SRCH",
  title: "Search and lookup results (filter with query_term=)",
  tabs: [
    opt("quotes", "Search quotes", "search_quotes"),
    mkt("lists", "Search lists", "search_lists"),
    mkt("reports", "Report hits", "search_report_hits"),
    opt("lookup", "Lookup results", "lookup_results"),
    mkt("totals", "Lookup totals", "lookup_totals"),
  ],
});

export const DOM_PANEL = tabbedPanel({
  code: "DOM",
  title: "Sectors and industries: taxonomy, metrics, top companies/funds/movers, research",
  tabs: [
    mkt("metrics", "Metrics", "domain_metrics"),
    sym("tree", "Taxonomy (by domain symbol)", "domains"),
    opt("companies", "Top companies", "domain_top_companies"),
    opt("funds", "Top funds", "domain_top_funds"),
    opt("movers", "Top movers", "domain_top_movers"),
    mkt("reports", "Research reports", "research_reports"),
    mkt("links", "Report links", "domain_report_links"),
  ],
});

export const REF_PANEL = tabbedPanel({
  code: "REF",
  title: "Reference snapshots: fast info, history metadata, identity history, officers, shares",
  tabs: [
    sym("fast", "Fast info", "fast_info"),
    sym("fasthist", "Fast info history", "fast_info_history"),
    sym("meta", "History metadata", "history_metadata"),
    sym("infohist", "Identity history", "info_history"),
    sym("officers", "Officers", "company_officers"),
    sym("shares", "Shares outstanding", "shares_full"),
    sym("newsmap", "News mentions", "news_symbols"),
  ],
});

export const CURATED: PanelSpec[] = [
  HDS_PANEL,
  ERN_PANEL,
  FUND_PANEL,
  CAL_PANEL,
  MKT_PANEL,
  SCR_PANEL,
  SRCH_PANEL,
  DOM_PANEL,
  REF_PANEL,
];
