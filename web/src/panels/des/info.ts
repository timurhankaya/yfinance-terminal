// What one `info` snapshot MEANS: which section a key belongs to, how a
// value is written, and which handful of keys are worth a card.
//
// Pure, and deliberately so. `DES.tsx` used to hold all of this beside
// its JSX, which made the one part worth testing on its own the part
// hardest to reach. Nothing here imports React.
//
// Keys are the API's: the `info` snapshot is normalised to snake_case on
// the way into the database, not yfinance's camelCase. Numbers arrive as
// strings (Decimal on the wire).
import { LOCALE, asNumber, formatBig } from "../format";
import { formatDateTime } from "../table";

//: Ratios Yahoo sends as fractions (0.24 is 24 %).
const PERCENT_FRACTION = new Set([
  "profit_margins", "gross_margins", "operating_margins", "ebitda_margins",
  "return_on_assets", "return_on_equity", "revenue_growth", "earnings_growth",
  "earnings_quarterly_growth", "held_percent_insiders", "held_percent_institutions",
  "short_percent_of_float", "payout_ratio", "trailing_annual_dividend_yield",
  "three_year_average_return", "five_year_average_return", "fund_yield",
]);
//: Values Yahoo already sends as percentages (0.34 is 0.34 %).
//:
//: `ytd_return` sits here and its own siblings do not, which looks like
//: an error and is not: measured over fifteen funds in the archive,
//: `ytd_return` is 13.72 / -36.19 / 19.54 while
//: `three_year_average_return` beside it is 0.2464 / -0.5708. Yahoo is
//: inconsistent inside one payload, so the classification is per key and
//: by measurement. It was in the fraction set and drew SPY's year as
//: 1307.29 %.
const PERCENT_ALREADY = new Set([
  "dividend_yield", "five_year_avg_dividend_yield", "net_expense_ratio", "ytd_return",
]);
//: Keys whose numeric value is a UNIX epoch (seconds).
const EPOCH_RE = /(_date|_timestamp|_timestamp_start|_timestamp_end|_time|_epoch_date|fiscal_year_end|most_recent_quarter)$/;

/** The section titles, in the order a snapshot is read. A group below
 *  names these, so the two lists are joined by value and a section that
 *  is renamed here without being renamed there fails a test rather than
 *  quietly disappearing from every tab. */
export const SECTIONS: ReadonlyArray<[title: string, keys: string[]]> = [
  ["Identity", [
    "quote_type", "type_disp", "short_name", "long_name", "display_name", "exchange", "full_exchange_name",
    "exchange_timezone_name", "exchange_timezone_short_name", "market", "region", "language", "market_state",
    "currency", "financial_currency", "quote_source_name", "message_board_id", "price_hint", "source_interval",
    "exchange_data_delayed_by", "gmt_offset_milliseconds", "sector", "sector_key", "sector_disp", "industry",
    "industry_key", "industry_disp", "category", "fund_family", "legal_type", "full_time_employees", "tradeable",
    "triggerable", "crypto_tradeable", "esg_populated", "has_pre_post_market_data",
  ]],
  ["Contact", ["address1", "address2", "city", "state", "zip", "country", "phone", "fax", "website", "ir_website"]],
  ["Price & volume", [
    "current_price", "regular_market_price", "regular_market_open", "regular_market_day_high",
    "regular_market_day_low", "regular_market_previous_close", "regular_market_change",
    "regular_market_change_percent", "regular_market_day_range", "previous_close", "open", "day_high", "day_low",
    "bid", "ask", "bid_size", "ask_size", "pre_market_price", "pre_market_change", "pre_market_change_percent",
    "pre_market_time", "regular_market_time", "fifty_day_average", "two_hundred_day_average",
    "fifty_two_week_high", "fifty_two_week_low", "fifty_two_week_range", "fifty_two_week_change_percent",
    "all_time_high", "all_time_low", "volume", "regular_market_volume", "average_volume", "average_volume_10days",
    "average_daily_volume_10day", "average_daily_volume_3month",
  ]],
  ["Valuation", [
    "market_cap", "non_diluted_market_cap", "enterprise_value", "trailing_pe", "forward_pe", "price_to_book",
    "price_to_sales_trailing_12_months", "peg_ratio", "trailing_peg_ratio", "enterprise_to_revenue",
    "enterprise_to_ebitda", "book_value", "trailing_eps", "forward_eps", "eps_trailing_twelve_months",
    "eps_current_year", "eps_forward", "price_eps_current_year", "revenue_per_share", "total_cash_per_share", "beta",
  ]],
  ["Income & cash flow", [
    "total_revenue", "gross_profits", "ebitda", "net_income_to_common", "free_cashflow", "operating_cashflow",
    "revenue_growth", "earnings_growth", "earnings_quarterly_growth",
  ]],
  ["Balance sheet", ["total_cash", "total_debt", "total_assets", "net_assets", "current_ratio", "quick_ratio", "debt_to_equity"]],
  ["Margins & returns", [
    "profit_margins", "gross_margins", "operating_margins", "ebitda_margins", "return_on_assets", "return_on_equity",
  ]],
  ["Share statistics", [
    "shares_outstanding", "implied_shares_outstanding", "float_shares", "shares_short", "shares_short_prior_month",
    "short_ratio", "short_percent_of_float", "held_percent_insiders", "held_percent_institutions",
    "date_short_interest", "shares_short_previous_month_date",
  ]],
  ["Dividends & splits", [
    "dividend_rate", "dividend_yield", "trailing_annual_dividend_rate", "trailing_annual_dividend_yield",
    "five_year_avg_dividend_yield", "payout_ratio", "last_dividend_value", "last_dividend_date", "ex_dividend_date",
    "dividend_date", "last_split_factor", "last_split_date",
  ]],
  ["Analyst view", [
    "recommendation_key", "recommendation_mean", "average_analyst_rating", "number_of_analyst_opinions",
    "target_high_price", "target_low_price", "target_mean_price", "target_median_price",
  ]],
  ["Key dates", [
    "first_trade_date", "earnings_timestamp", "earnings_timestamp_start", "earnings_timestamp_end",
    "earnings_call_timestamp_start", "earnings_call_timestamp_end", "is_earnings_date_estimate",
    "last_fiscal_year_end", "next_fiscal_year_end", "most_recent_quarter", "governance_epoch_date",
    "compensation_as_of_epoch_date", "start_date",
  ]],
  ["Fund", [
    "net_expense_ratio", "ytd_return", "three_year_average_return", "five_year_average_return", "beta_3_year",
    "nav_price", "fund_yield", "fund_inception_date",
  ]],
  ["Crypto", [
    "from_currency", "to_currency", "last_market", "circulating_supply", "max_supply", "total_supply",
    "fully_diluted_value", "volume_24hr", "volume_all_currencies",
  ]],
];

//: Where a key the sections do not name lands. Named, because the tab
//: that collects the leftovers has to ask for it by value.
export const OTHER = "Other";

//: Shown as its own paragraph, not a definition row.
export const SUMMARY_KEY = "long_business_summary";
//: Already in the identity block above the sections.
const IDENTITY_KEYS = new Set(["symbol"]);

export function label(key: string): string {
  const words = key.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function epochToText(key: string, n: number): string {
  // Seconds since 1970; a value this small as milliseconds would be 1970.
  const date = new Date(n * 1000);
  if (Number.isNaN(date.getTime())) return String(n);
  return key.endsWith("_time") ? formatDateTime(date.toISOString()) : date.toISOString().slice(0, 10);
}

const ISO_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/;
//: Integer years, shown without separators. By name, not by a pattern:
//: `three_year_average_return` is a ratio and `last_fiscal_year_end` an
//: epoch, and both contain "year".
const YEAR_KEYS = new Set(["year_born", "fiscal_year"]);

/** One info value as text, by what its key says it is. */
//: Digits that are not numbers: a postal code of 95014 is not 95,014.
const TEXT_KEYS = new Set(["zip", "phone", "fax", "address1", "address2", "message_board_id", "price_hint"]);

export function formatInfo(key: string, value: unknown): string {
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (TEXT_KEYS.has(key)) return String(value);
  // The pipeline already turned most epochs into ISO strings.
  if (typeof value === "string" && ISO_RE.test(value)) return formatDateTime(value);
  const n = asNumber(value);
  if (n === null) return String(value);
  if (PERCENT_FRACTION.has(key)) return `${(n * 100).toFixed(2)}%`;
  if (PERCENT_ALREADY.has(key) || key.endsWith("_percent")) return `${n.toFixed(2)}%`;
  if (EPOCH_RE.test(key) && n > 1e8) return epochToText(key, n);
  if (YEAR_KEYS.has(key) && Number.isInteger(n)) return String(n);
  if (Math.abs(n) >= 1e6) return formatBig(n);
  // A fixed locale: the terminal reads the same on a tr-TR machine as on
  // an en-US one, and 36,61 next to 4.67T would be two number formats.
  if (Number.isInteger(n)) return n.toLocaleString(LOCALE);
  return n.toLocaleString(LOCALE, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export type Entry = [key: string, value: unknown];
export type Section = [title: string, entries: Entry[]];

export interface Grouped {
  sections: Section[];
  summary: string | null;
  nulls: number;
}

/** Every non-null key of `info` placed in its section, the rest in "Other". */
export function group(info: Record<string, unknown>): Grouped {
  const placed = new Set<string>([SUMMARY_KEY, ...IDENTITY_KEYS]);
  const sections: Section[] = [];
  let nulls = 0;
  const present = (key: string) => info[key] !== null && info[key] !== undefined;
  for (const [title, keys] of SECTIONS) {
    const entries: Entry[] = [];
    for (const key of keys) {
      placed.add(key);
      if (key in info && present(key)) entries.push([key, info[key]]);
    }
    if (entries.length > 0) sections.push([title, entries]);
  }
  const other: Entry[] = [];
  for (const key of Object.keys(info)) {
    if (!present(key)) {
      nulls += 1;
      continue;
    }
    if (!placed.has(key)) other.push([key, info[key]]);
  }
  if (other.length > 0) sections.push([OTHER, other]);
  const summary = info[SUMMARY_KEY];
  return { sections, summary: typeof summary === "string" ? summary : null, nulls };
}
