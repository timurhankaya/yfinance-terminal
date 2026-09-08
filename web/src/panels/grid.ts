// Which columns a dataset puts in its GRID. The rest are not dropped --
// they are in the row detail, one click away, which is what the detail
// is for.
//
// The archive is wide: `screen_quotes` has 106 columns, `info_history`
// 190, and a dozen more are over fifteen. A grid that draws all of them
// is a horizontal scrollbar with a table somewhere behind it, and the
// reader cannot see two rows of the same column at once -- which is the
// one thing a table is for.
//
// Two rules, in order:
//   1. A dataset named in `GRID` shows exactly those columns, in that
//      order. This is where a real editorial choice lives: which six
//      columns answer the question this dataset exists to answer.
//   2. Anything else shows every column except the housekeeping ones.
//
// The order matters, and `*_history` is why. `fetched_at` is bookkeeping
// on a current-state table and the TIME AXIS on a history one, so a
// blanket filter would gut exactly the tables that need it most. The
// history datasets name it in rule 1 and get it back.
import type { CatalogColumn } from "../api/client";

/** Columns that say when and how the archive wrote the row rather than
 *  what the row says. Every one of them is still in the row detail. */
export const HOUSEKEEPING: ReadonlySet<string> = new Set([
  "raw_json", "fetched_at", "content_hash", "is_known", "fact_hash", "url_hash", "first_seen_at",
]);

//: Per dataset, the grid's columns in the grid's order. Six or seven at
//: most: past that a reader is scanning rather than reading, and the
//: detail is one Enter away.
export const GRID: Readonly<Record<string, readonly string[]>> = {
  // --- symbol ---------------------------------------------------------
  company_officers: ["name", "title", "age", "fiscal_year", "total_pay"],
  earnings_estimate: ["period", "metric", "avg", "low", "high", "number_of_analysts", "growth"],
  eps_trend: ["period", "current", "days_ago_7", "days_ago_30", "days_ago_60", "days_ago_90"],
  fast_info: ["last_price", "previous_close", "day_low", "day_high", "market_cap", "last_volume", "year_change"],
  fast_info_history: ["fetched_at", "last_price", "previous_close", "day_low", "day_high", "market_cap", "last_volume"],
  fund_profile: ["as_of_date", "category_name", "family", "expense_ratio", "total_net_assets", "stock_position", "bond_position"],
  history_metadata: ["instrument_type", "exchange_name", "currency", "regular_market_price", "fifty_two_week_high", "fifty_two_week_low", "first_trade_date"],
  info_history: ["fetched_at", "short_name", "market_state", "current_price", "market_cap", "trailing_pe", "dividend_yield"],
  insider_purchases: ["period_label", "purchases_shares", "sales_shares", "net_shares", "net_pct", "buy_pct"],
  insider_roster_holders: ["name", "position", "most_recent_transaction", "latest_transaction_date", "shares_owned_directly", "shares_owned_indirectly"],
  insider_transactions: ["start_date", "insider", "position", "transaction_label", "shares", "value"],
  institutional_holders: ["holder", "date_reported", "pct_held", "pct_change", "shares", "value"],
  lookup_results: ["rank_index", "symbol", "short_name", "quote_type", "exchange", "regular_market_price", "regular_market_percent_change"],
  mutualfund_holders: ["holder", "date_reported", "pct_held", "pct_change", "shares", "value"],
  news: ["pub_date", "title", "provider_name", "content_type"],
  option_quotes: ["expiry_date", "option_type", "strike", "last_price", "bid", "ask", "volume", "open_interest"],
  revenue_estimate: ["period", "metric", "avg", "low", "high", "number_of_analysts", "growth"],
  search_lists: ["name", "list_type", "symbol_count", "daily_percent_gain", "follower_count", "score"],
  search_quotes: ["symbol", "short_name", "quote_type", "exchange", "sector", "industry", "score"],
  ticker_calendar: ["earnings_date_start", "earnings_date_end", "dividend_date", "ex_dividend_date", "earnings_average", "revenue_average"],
  ticker_calendar_history: ["fetched_at", "earnings_date_start", "dividend_date", "ex_dividend_date", "earnings_average", "revenue_average"],
  upgrades_downgrades: ["grade_ts_utc", "firm", "from_grade", "to_grade", "action", "current_price_target"],

  // --- market ---------------------------------------------------------
  earnings_calendar: ["event_start_ts_utc", "symbol", "company", "eps_estimate", "reported_eps", "surprise_pct"],
  ipo_calendar: ["ipo_date_utc", "symbol", "company", "exchange", "action", "price", "shares"],
  market_status: ["region", "name", "status", "open_ts_utc", "close_ts_utc", "timezone_name"],
  market_status_history: ["fetched_at", "region", "name", "status", "open_ts_utc", "close_ts_utc"],
  market_summary: ["symbol", "short_name", "market_state", "regular_market_price", "regular_market_change", "regular_market_change_percent"],
  market_summary_history: ["fetched_at", "symbol", "short_name", "regular_market_price", "regular_market_change", "regular_market_change_percent"],
  screen_quotes: ["symbol", "short_name", "regular_market_price", "regular_market_change_percent", "regular_market_volume", "market_cap", "trailing_pe"],
  screen_runs: ["as_of_date", "total", "fetched_rows", "row_count", "page_count", "last_updated"],
  screens: ["screen_key", "title", "kind", "quote_type", "sort_field", "is_enabled"],

  // --- domain ---------------------------------------------------------
  domain_metrics: ["domain_key", "as_of_date", "market_cap", "market_weight", "ytd_change_pct", "reg_market_change_pct", "one_year_change_pct"],
  domain_top_companies: ["symbol", "name", "market_weight", "market_cap", "last_price", "target_price", "ytd_return"],
  domain_top_funds: ["symbol", "name", "net_assets", "expense_ratio", "last_price", "ytd_return"],
  domain_top_movers: ["rank_type", "symbol", "name", "last_price", "ytd_return", "growth_estimate"],
  research_reports: ["report_ts_utc", "provider", "report_title", "investment_rating", "target_price", "report_type"],
};

/** The grid's columns for one dataset: its own list where it has one,
 *  everything but the housekeeping otherwise.
 *
 *  `drop` is the panel's own subtraction -- a symbol column that only
 *  repeats the band above it. It applies to rule 2 only: a dataset that
 *  names its grid has already said whether the symbol belongs in it. */
export function gridNames(
  dataset: string,
  columns: readonly CatalogColumn[],
  drop: readonly string[] = [],
): string[] {
  const have = new Set(columns.map((column) => column.name));
  const named = GRID[dataset];
  // Filtered against the catalogue rather than trusted: a column this
  // file names and the archive has since renamed must leave a gap in the
  // grid, not an empty column with a heading.
  if (named !== undefined) return named.filter((name) => have.has(name));
  const dropped = new Set([...drop, ...HOUSEKEEPING]);
  return columns.map((column) => column.name).filter((name) => !dropped.has(name));
}
