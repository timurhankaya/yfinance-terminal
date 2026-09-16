// How the snapshot is LAID OUT: which sections share a tab, which keys
// earn a card, and which pictures the snapshot can draw without a second
// request. Separate from `info.ts`: that file says what a key means; this
// is a presentation decision. Both are pure, so the arithmetic is
// testable without rendering.
import { Interval } from "../../api/client";
import { asNumber } from "../format";
import { OTHER, type Grouped, type Section } from "./info";

/** The four tabs the sections collapse into, one per question a reader
 *  arrives with: what is it doing, what is it worth, who owns it, what is
 *  it. */
export enum FieldTab {
  Price = "price",
  Fundamentals = "fundamentals",
  Ownership = "ownership",
  Reference = "reference",
}

export function isFieldTab(value: string): value is FieldTab {
  return (Object.values(FieldTab) as string[]).includes(value);
}

//: Section titles, by value, from `SECTIONS` in `info.ts`. Every title
//: appears exactly once across the four, and `info.test.ts` asserts it:
//: a section named here but not there would silently show nothing, and
//: one named there but not here would vanish from the panel.
export const MEMBERS: ReadonlyArray<[FieldTab, label: string, titles: string[]]> = [
  [FieldTab.Price, "Price", ["Price & volume"]],
  [FieldTab.Fundamentals, "Fundamentals", [
    "Valuation", "Income & cash flow", "Balance sheet", "Margins & returns", "Analyst view",
  ]],
  [FieldTab.Ownership, "Ownership", ["Share statistics", "Dividends & splits"]],
  [FieldTab.Reference, "Reference", ["Identity", "Contact", "Key dates", "Fund", "Crypto", OTHER]],
];

export interface TabView {
  key: FieldTab;
  label: string;
  /** The sections this tab shows, in `SECTIONS` order, already filtered
   *  to the ones the snapshot filled. */
  sections: Section[];
  /** Fields under the tab, which is what the count beside it says. */
  fields: number;
}

/** The four tabs, each carrying whatever sections the snapshot filled.
 *
 *  All four are returned even when empty: a tab that comes and goes with
 *  the symbol would move the others under the reader's cursor, and an
 *  empty tab that says so is a fact about the snapshot. */
export function tabsOf(grouped: Grouped): TabView[] {
  const bySection = new Map(grouped.sections.map((section) => [section[0], section]));
  return MEMBERS.map(([key, label, titles]) => {
    const sections = titles.flatMap((title) => {
      const found = bySection.get(title);
      return found === undefined ? [] : [found];
    });
    return { key, label, sections, fields: sections.reduce((sum, [, e]) => sum + e.length, 0) };
  });
}

/** The tab to open when the address does not name one: the first with
 *  anything in it. A crypto pair has no Fundamentals and an index has no
 *  Ownership, and neither should open on a blank tab. */
export function firstFilled(tabs: TabView[]): FieldTab {
  return tabs.find((tab) => tab.fields > 0)?.key ?? FieldTab.Price;
}

// --- the cards ---------------------------------------------------------------

export interface Stat {
  key: string;
  label: string;
  /** Whether the sign of the value is the point, so it can be coloured.
   *  A market cap is never green. */
  signed: boolean;
}

//: Candidates in priority order, across the quote types: an equity fills
//: the first few, a fund the middle, a crypto pair the tail. The first
//: `STAT_CARDS` the snapshot actually has are the ones drawn, so a fund
//: is not given a row of dashes where an equity's ratios would be.
const CANDIDATES: ReadonlyArray<[key: string, label: string, signed?: true]> = [
  ["market_cap", "Market cap"],
  ["trailing_pe", "P/E (TTM)"],
  ["forward_pe", "P/E (fwd)"],
  ["price_to_book", "P/B"],
  ["dividend_yield", "Dividend yield"],
  ["beta", "Beta"],
  ["fifty_two_week_change_percent", "52w change", true],
  ["profit_margins", "Profit margin", true],
  ["revenue_growth", "Revenue growth", true],
  ["net_expense_ratio", "Expense ratio"],
  ["ytd_return", "YTD return", true],
  ["total_assets", "Total assets"],
  ["net_assets", "Net assets"],
  ["nav_price", "NAV"],
  ["circulating_supply", "Circulating supply"],
  ["max_supply", "Max supply"],
  ["volume_24hr", "24h volume"],
  ["average_volume", "Avg volume"],
  ["volume", "Volume"],
];

//: Six, because the grid is `minmax(132px, 1fr)`: six fit on one row of a
//: full-width panel and wrap to two of three in a split. A seventh would
//: leave a widow on most widths.
export const STAT_CARDS = 6;

/** The cards, in candidate order, for the keys this snapshot filled. */
export function statsOf(info: Record<string, unknown>, limit: number = STAT_CARDS): Stat[] {
  const stats: Stat[] = [];
  for (const [key, label, signed] of CANDIDATES) {
    const value = info[key];
    if (value === null || value === undefined) continue;
    stats.push({ key, label, signed: signed === true });
    if (stats.length === limit) break;
  }
  return stats;
}

/** The sign of a stat, as the class name the terminal colours it with,
 *  or undefined where the sign carries nothing. A grey zero is a fact, a
 *  green one is a story. */
export function statDirection(stat: Stat, value: unknown): string | undefined {
  if (!stat.signed) return undefined;
  const n = asNumber(value);
  if (n === null || n === 0) return undefined;
  return n > 0 ? "up" : "down";
}

// --- the two bullets ---------------------------------------------------------

export interface RangeMark {
  low: number;
  high: number;
  mean: number;
  actual: number | null;
  label: string;
}

function pick(info: Record<string, unknown>, ...keys: string[]): number | null {
  for (const key of keys) {
    const value = asNumber(info[key]);
    if (value !== null) return value;
  }
  return null;
}

/** Where the price sits in its own year.
 *
 *  The tick inside the range is a moving average rather than the
 *  midpoint: a midpoint is arithmetic the reader can already see, and the
 *  200-day is the line they would have drawn. */
export function yearRange(info: Record<string, unknown>): RangeMark | null {
  const low = pick(info, "fifty_two_week_low");
  const high = pick(info, "fifty_two_week_high");
  const mean = pick(info, "two_hundred_day_average", "fifty_day_average");
  if (low === null || high === null || mean === null || high <= low) return null;
  return {
    low, high, mean,
    actual: pick(info, "current_price", "regular_market_price"),
    label: info.two_hundred_day_average === undefined || info.two_hundred_day_average === null
      ? "52-week range, with the 50-day average"
      : "52-week range, with the 200-day average",
  };
}

/** Where the price sits against what analysts said it was worth. */
export function analystRange(info: Record<string, unknown>): RangeMark | null {
  const low = pick(info, "target_low_price");
  const high = pick(info, "target_high_price");
  const mean = pick(info, "target_mean_price", "target_median_price");
  if (low === null || high === null || mean === null || high <= low) return null;
  return {
    low, high, mean,
    actual: pick(info, "current_price", "regular_market_price"),
    label: "Analyst targets, low to high, with the consensus",
  };
}

// --- the margin bars ---------------------------------------------------------

const MARGINS: ReadonlyArray<[key: string, label: string]> = [
  ["gross_margins", "Gross"],
  ["operating_margins", "Operating"],
  ["ebitda_margins", "EBITDA"],
  ["profit_margins", "Net"],
];

export interface MarginBars {
  categories: string[];
  /** Percentages, not the fractions the snapshot stores (0.2431 -> 24.31). */
  percents: Array<number | null>;
}

/** The four margins as one picture: what happens to a dollar of revenue
 *  on its way down the statement. Four rows of a table make the reader
 *  do that subtraction themselves. */
export function marginBars(info: Record<string, unknown>): MarginBars | null {
  const found = MARGINS.flatMap(([key, label]) => {
    const value = asNumber(info[key]);
    return value === null ? [] : [{ label, percent: value * 100 }];
  });
  // Two, not one: a single bar is a number with ink around it.
  if (found.length < 2) return null;
  return { categories: found.map((m) => m.label), percents: found.map((m) => m.percent) };
}

// --- the trend window --------------------------------------------------------

/** The four windows the header trend offers. Not a free window: the card
 *  is a glance, and `GP` is where a reader picks a range. */
export enum TrendRange {
  D1 = "1d",
  W1 = "1w",
  M1 = "1m",
  Y1 = "1y",
}

export const TREND_RANGES: readonly TrendRange[] = Object.values(TrendRange);

export function isTrendRange(value: string): value is TrendRange {
  return (TREND_RANGES as string[]).includes(value);
}

export interface TrendWindow {
  interval: Interval;
  /** Calendar days asked for. Generous on both intraday windows: a
   *  Monday morning's "last day" is Friday, and a week over a holiday is
   *  four sessions. The tail is cut once the bars are here. */
  days: number;
  /** Whether to keep only the newest session's bars, which is what makes
   *  `1d` one day rather than the last six of them. */
  session: boolean;
  /** What the card is titled. */
  label: string;
}

//: Intraday for the two short windows and daily for the two long ones,
//: which is where the archive keeps them: `price_bars` is the intraday
//: table and `price_history` the daily one.
export const TREND_WINDOW: Record<TrendRange, TrendWindow> = {
  [TrendRange.D1]: { interval: Interval.M5, days: 6, session: true, label: "Today, 5-minute bars" },
  [TrendRange.W1]: { interval: Interval.M60, days: 9, session: false, label: "One week, hourly bars" },
  [TrendRange.M1]: { interval: Interval.D1, days: 34, session: false, label: "One month, daily closes" },
  [TrendRange.Y1]: { interval: Interval.D1, days: 372, session: false, label: "One year, daily closes" },
};

/** The closes a window's bars draw, oldest first. `session` cuts to the
 *  newest `session_date` rather than a bar count: sessions differ in
 *  length by exchange and on half days. */
export function closesOf(rows: ReadonlyArray<Record<string, unknown>>, session: boolean): number[] {
  let wanted = rows;
  if (session) {
    const dates = rows.map((row) => String(row.session_date ?? ""));
    const newest = dates.reduce((a, b) => (b > a ? b : a), "");
    if (newest !== "") wanted = rows.filter((row) => String(row.session_date ?? "") === newest);
  }
  return wanted.flatMap((row) => {
    const close = asNumber(row.close);
    return close === null ? [] : [close];
  });
}
