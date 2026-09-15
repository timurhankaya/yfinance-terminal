import type { PanelSpec } from "../commands/types";

const FUNCTIONS: Record<string, string> = {
  DES: "Company overview, key ratios, valuation, ownership and reference data.",
  GP: "Explore price candles and volume across intervals, with dividends and splits.",
  FA: "Compare income, balance sheet and cash flow across reporting periods.",
  ANR: "Analyst recommendations, price targets and rating changes.",
  PX: "Inspect historical open, high, low, close and volume in a sortable table.",
  QR: "Follow live trades, their timestamps, prices and sizes.",
  N: "Read the latest headlines and open the original articles.",
  EQS: "Browse equity screens, matching symbols and previous screening runs.",
  COMP: "Compare symbol performance from a common starting value of 100.",
  WLA: "Track prices, daily changes and volume for your list of symbols.",
  PG: "Open, save and manage your multi-panel workspace layouts.",
};
export function functionHelp(panel: PanelSpec): string {
  return `${panel.code} · ${panel.title}\n${FUNCTIONS[panel.code] ?? (panel.needsSymbol ? "Explore this data for the selected symbol." : "Explore market-wide data and tools.")}`;
}

export const FIELD_HELP: Record<string, string> = {
  price: "Price · Latest price, volume, trading ranges and market session data.",
  fundamentals: "Fundamentals · Valuation ratios, earnings, cash flow, financial health and analyst estimates.",
  ownership: "Ownership · Share counts, short interest, dividends and stock splits.",
  reference: "Reference · Company identity, contacts, key dates, fund details and officers.",
};
export const STATEMENT_HELP: Record<string, string> = {
  income: "Income statement · Revenue, expenses and profit for each reporting period.",
  balance_sheet: "Balance sheet · Assets, liabilities and shareholders’ equity at each period end.",
  cash_flow: "Cash flow · Cash generated and spent through operations, investing and financing.",
  annual: "Annual · Compare full fiscal years.",
  quarterly: "Quarterly · Compare individual fiscal quarters.",
  ttm: "TTM · Trailing twelve months, combining the latest four quarters.",
};

export const DATASET_HELP: Record<string, string> = {
  major_holders: "Ownership split between institutions, insiders and other holders.",
  institutional_holders: "Institutional investors, reported positions and ownership percentages.",
  mutualfund_holders: "Mutual funds holding the symbol and their reported positions.",
  insider_roster_holders: "Company insiders and their reported holdings.",
  insider_transactions: "Reported insider purchases, sales and transaction dates.",
  insider_purchases: "Aggregate insider buying and selling activity.",
  earnings_dates: "Scheduled and reported earnings dates, estimates and surprises.",
  earnings_history: "Reported earnings compared with analyst expectations.",
  earnings_estimate: "Analyst earnings-per-share forecasts for upcoming periods.",
  revenue_estimate: "Analyst sales forecasts for upcoming periods.",
  eps_trend: "How earnings-per-share forecasts have changed over time.",
  eps_revisions: "Upward and downward revisions to earnings forecasts.",
  growth_estimates: "Expected growth across forecast periods.",
  ticker_calendar: "Upcoming earnings and dividend dates for the symbol.",
  ticker_calendar_history: "Previous snapshots of upcoming company events.",
  fund_profile: "Fund family, category and investment profile.",
  fund_top_holdings: "The fund’s largest positions and portfolio weights.",
  fund_weightings: "Portfolio allocation by asset class and sector.",
  fund_metrics: "Fund valuation, risk and performance measures.",
  earnings_calendar: "Company earnings announcements across the market.",
  economic_calendar: "Scheduled economic releases and reported indicators.",
  ipo_calendar: "Upcoming and recent initial public offerings.",
  splits_calendar: "Scheduled stock splits and their ratios.",
  market_status: "Current trading sessions and exchange status.",
  market_summary: "Headline index and market quotes, changes and session details.",
  market_status_history: "Archived snapshots of exchange trading status.",
  market_summary_history: "Historical snapshots of headline market quotes.",
  domain_metrics: "Sector and industry valuation, size and performance measures.",
  domains: "Sector and industry classifications for the selected domain.",
  domain_top_companies: "Leading companies within a sector or industry.",
  domain_top_funds: "Leading funds associated with a sector or industry.",
  domain_top_movers: "Top price movers within a sector or industry.",
  research_reports: "Research reports available in the archive.",
  domain_report_links: "Source links for sector and industry research.",
  fast_info: "A compact snapshot of price, volume and key market facts.",
  fast_info_history: "Previous snapshots of key market facts.",
  history_metadata: "Trading hours, timezone and price-history context.",
  info_history: "Archived company profile and financial snapshots.",
  company_officers: "Company executives, roles and reported compensation.",
  shares_full: "Shares outstanding over time.",
  news_symbols: "Articles in the archive that mention the selected symbol.",
};
