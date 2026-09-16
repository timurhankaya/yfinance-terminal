import { describe, expect, it } from "vitest";
import { OTHER, SECTIONS, group } from "./info";
import {
  FieldTab,
  MEMBERS,
  STAT_CARDS,
  analystRange,
  firstFilled,
  isFieldTab,
  marginBars,
  statDirection,
  statsOf,
  tabsOf,
  yearRange,
} from "./layout";

describe("the tabs and the sections", () => {
  // The one invariant worth a test of its own: the two lists are joined
  // by section TITLE, and nothing but a test notices when they drift. A
  // section named in `SECTIONS` and not in a tab would be fetched,
  // grouped and then shown nowhere.
  it("claims every section exactly once, and claims nothing that does not exist", () => {
    const titles = [...SECTIONS.map(([title]) => title), OTHER];
    const claimed = MEMBERS.flatMap(([, , sections]) => sections);
    expect([...claimed].sort()).toEqual([...titles].sort());
    expect(new Set(claimed).size).toBe(claimed.length);
  });

  it("puts each filled section under its tab and counts the fields", () => {
    const tabs = tabsOf(group({ open: "1", market_cap: "2", trailing_pe: "3", sector: "Tech" }));
    const byKey = new Map(tabs.map((tab) => [tab.key, tab]));
    expect(byKey.get(FieldTab.Price)?.fields).toBe(1);
    expect(byKey.get(FieldTab.Fundamentals)?.fields).toBe(2);
    expect(byKey.get(FieldTab.Ownership)?.fields).toBe(0);
    expect(byKey.get(FieldTab.Reference)?.fields).toBe(1);
    expect(byKey.get(FieldTab.Fundamentals)?.sections.map(([t]) => t)).toEqual(["Valuation"]);
  });

  it("returns all four tabs even when empty, so none moves under the cursor", () => {
    expect(tabsOf(group({})).map((tab) => tab.key)).toEqual(Object.values(FieldTab));
  });

  it("opens on the first tab with anything in it", () => {
    // A crypto pair: no price fields, no valuation, everything under
    // Reference. Opening on Price would be a blank first screen.
    expect(firstFilled(tabsOf(group({ from_currency: "BTC" })))).toBe(FieldTab.Reference);
    expect(firstFilled(tabsOf(group({ open: "1", sector: "Tech" })))).toBe(FieldTab.Price);
    expect(firstFilled(tabsOf(group({})))).toBe(FieldTab.Price);
  });

  it("recognises the tab names an address can carry", () => {
    expect(isFieldTab("ownership")).toBe(true);
    expect(isFieldTab("Ownership")).toBe(false);
    expect(isFieldTab("holders")).toBe(false);
  });
});

describe("the cards", () => {
  it("takes the first six keys the snapshot filled, in candidate order", () => {
    const stats = statsOf({
      market_cap: "1", trailing_pe: "2", forward_pe: "3", price_to_book: "4",
      dividend_yield: "5", beta: "6", fifty_two_week_change_percent: "7", volume: "8",
    });
    expect(stats).toHaveLength(STAT_CARDS);
    expect(stats.map((s) => s.key)).toEqual([
      "market_cap", "trailing_pe", "forward_pe", "price_to_book", "dividend_yield", "beta",
    ]);
  });

  it("skips what the snapshot does not have rather than drawing a dash", () => {
    // A fund: none of an equity's leading ratios, so its own keys are
    // what reach the cards.
    const stats = statsOf({ market_cap: null, trailing_pe: undefined, net_expense_ratio: "0.03", nav_price: "52.1" });
    expect(stats.map((s) => s.key)).toEqual(["net_expense_ratio", "nav_price"]);
  });

  it("colours only the values whose sign is the point", () => {
    const [change] = statsOf({ fifty_two_week_change_percent: "-3" });
    const [cap] = statsOf({ market_cap: "-3" });
    expect(statDirection(change!, "-3")).toBe("down");
    expect(statDirection(change!, "3")).toBe("up");
    expect(statDirection(change!, "0")).toBeUndefined();
    expect(statDirection(cap!, "-3")).toBeUndefined();
  });
});

describe("the two bullets", () => {
  it("draws the year range with the longest average the snapshot has", () => {
    const range = yearRange({
      fifty_two_week_low: "100", fifty_two_week_high: "200",
      two_hundred_day_average: "150", fifty_day_average: "180", current_price: "175",
    });
    expect(range).toEqual({ low: 100, high: 200, mean: 150, actual: 175, label: expect.stringContaining("200-day") });
    const shorter = yearRange({ fifty_two_week_low: "100", fifty_two_week_high: "200", fifty_day_average: "180" });
    expect(shorter?.mean).toBe(180);
    expect(shorter?.label).toContain("50-day");
    expect(shorter?.actual).toBeNull();
  });

  it("draws nothing without a range to draw", () => {
    expect(yearRange({ fifty_two_week_low: "100", two_hundred_day_average: "150" })).toBeNull();
    // An inverted or flat range is a bad snapshot, not a bullet.
    expect(yearRange({ fifty_two_week_low: "200", fifty_two_week_high: "100", fifty_day_average: "150" })).toBeNull();
  });

  it("falls back to the median target when there is no mean", () => {
    const range = analystRange({ target_low_price: "200", target_high_price: "300", target_median_price: "260" });
    expect(range?.mean).toBe(260);
    expect(analystRange({ target_low_price: "200", target_high_price: "300" })).toBeNull();
  });
});

describe("the margin bars", () => {
  it("turns the fractions the snapshot stores into percentages, down the statement", () => {
    const bars = marginBars({ gross_margins: "0.4652", operating_margins: "0.3", profit_margins: "0.2431" });
    expect(bars?.categories).toEqual(["Gross", "Operating", "Net"]);
    expect(bars?.percents.map((p) => Number(p!.toFixed(2)))).toEqual([46.52, 30, 24.31]);
  });

  it("needs two margins: one bar is a number with ink around it", () => {
    expect(marginBars({ profit_margins: "0.24" })).toBeNull();
    expect(marginBars({})).toBeNull();
  });
});
