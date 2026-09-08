import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DES, DES_PANEL, formatInfo, group } from "./DES";
import { FieldTab, TrendRange } from "./des/layout";
import { formatBig } from "./format";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function LocationProbe() {
  const { pathname, search } = useLocation();
  return <span data-testid="location">{pathname + search}</span>;
}

// vitest.config.ts sets `globals: false`, so @testing-library/react's
// automatic afterEach(cleanup) (which looks for a global `afterEach`)
// never registers; unmount explicitly so tests stay isolated.
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

//: Shaped like the real API: snake_case keys, Decimal numbers as
//: strings, dividend_yield already a percentage.
const INFO = {
  sector: "Technology", industry: "Consumer Electronics",
  market_cap: "4669700046848", trailing_pe: "36.609840000000",
  dividend_yield: "0.340000000000", website: "https://apple.com",
  beta: null, brand_new_key: "kept",
};

//: Two sessions of bars, so the `1d` window has something to cut away.
const BARS = [
  { session_date: "2026-09-03", close: "300" },
  { session_date: "2026-09-03", close: "305" },
  { session_date: "2026-09-04", close: "310" },
  { session_date: "2026-09-04", close: "320" },
];

function mockApi(info: Record<string, unknown> | null = INFO, bars: Array<Record<string, unknown>> = BARS, seen: string[] = []) {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    seen.push(url);
    if (url.startsWith("/ui/api/v1/datasets/company_officers")) return json(200, { data: [], next_cursor: null });
    if (url.startsWith("/ui/api/v1/symbols/AAPL/bars")) return json(200, { data: bars, next_cursor: null });
    if (url === "/ui/api/v1/symbols/AAPL") return json(200, { data: {
      symbol: "AAPL", long_name: "Apple Inc.", short_name: "Apple", exchange: "NMS",
      full_exchange_name: "NasdaqGS", currency: "USD", quote_type: "EQUITY",
      timezone: "America/New_York", is_active: true, info,
    } });
    throw new Error(`unexpected ${url}`);
  });
}

function renderDES(symbol: string, args: Record<string, string> = {}) {
  return render(
    <MemoryRouter initialEntries={[`/ui/t/${symbol}/DES`]}>
      <LocationProbe />
      <DES symbol={symbol} args={args} />
    </MemoryRouter>,
  );
}

describe("formatBig", () => {
  it("scales to K/M/B/T with two decimals", () => {
    expect(formatBig(3_500_000_000_000)).toBe("3.50T");
    expect(formatBig(12_345_678)).toBe("12.35M");
    expect(formatBig(999)).toBe("999");
  });
});

describe("DES", () => {
  it("opens on an overview: the name, its facts, and the numbers worth a card", async () => {
    mockApi();
    const { container } = renderDES("AAPL");
    expect(await screen.findByText("Apple Inc.")).toBeInTheDocument();
    // The identity that used to be seven rows of a definition list.
    for (const chip of ["AAPL", "NasdaqGS", "EQUITY", "USD", "America/New_York"]) {
      expect(screen.getByText(chip)).toBeInTheDocument();
    }
    const stats = container.querySelector(".stats")!;
    expect(within(stats as HTMLElement).getByText("4.67T")).toBeInTheDocument();
    expect(within(stats as HTMLElement).getByText("36.61")).toBeInTheDocument();
    expect(within(stats as HTMLElement).getByText("0.34%")).toBeInTheDocument();
  });

  it("opens on the first tab the snapshot filled, and counts every tab's fields", async () => {
    mockApi();
    renderDES("AAPL");
    await screen.findByText("Apple Inc.");
    // No price fields in this snapshot, so Price is not where it opens.
    const price = screen.getByRole("tab", { name: /Price/ });
    expect(price.getAttribute("aria-selected")).toBe("false");
    expect(within(price).getByText("0")).toBeInTheDocument();
    const fundamentals = screen.getByRole("tab", { name: /Fundamentals/ });
    expect(fundamentals.getAttribute("aria-selected")).toBe("true");
    // market_cap and trailing_pe, both under Valuation.
    expect(within(fundamentals).getByText("2")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Valuation" })).toBeInTheDocument();
    expect(screen.getByText("Trailing pe")).toBeInTheDocument();
  });

  it("keeps every field the snapshot carried, one tab down", async () => {
    mockApi();
    renderDES("AAPL", { tab: FieldTab.Reference });
    expect(await screen.findByText("Apple Inc.")).toBeInTheDocument();
    // A key the sections know, a key they do not, and a link.
    expect(screen.getByText("Technology")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Other" })).toBeInTheDocument();
    expect(screen.getByText("Brand new key")).toBeInTheDocument();
    expect(screen.getByText("kept")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "https://apple.com" })).toBeInTheDocument();
    // A null field is left out and counted, on every tab.
    expect(screen.queryByText("Beta")).not.toBeInTheDocument();
    expect(screen.getByText(/1 empty field/)).toBeInTheDocument();
  });

  it("switches tabs through the address, like every other argument", async () => {
    mockApi();
    renderDES("AAPL");
    await screen.findByText("Apple Inc.");
    fireEvent.click(screen.getByRole("tab", { name: /Ownership/ }));
    expect(screen.getByTestId("location").textContent).toBe("/ui/t/AAPL/DES?tab=ownership");
  });

  it("says a tab is empty rather than showing a blank page", async () => {
    mockApi();
    renderDES("AAPL", { tab: FieldTab.Price });
    expect(await screen.findByText(/no price fields/)).toBeInTheDocument();
  });

  it("opens the trend on the month, over daily bars", async () => {
    const seen: string[] = [];
    mockApi(INFO, BARS, seen);
    renderDES("AAPL");
    expect(await screen.findByRole("img", { name: /one month, daily closes, 4 bars, up/i })).toBeInTheDocument();
    const bars = seen.find((url) => url.includes("/symbols/AAPL/bars"))!;
    expect(bars).toContain("interval=1d");
    expect(screen.getByRole("button", { name: "1M" })).toHaveAttribute("aria-pressed", "true");
  });

  it("cuts the day window to the newest session, over intraday bars", async () => {
    const seen: string[] = [];
    mockApi(INFO, BARS, seen);
    renderDES("AAPL", { range: TrendRange.D1 });
    // Two of the four bars are the day before, and a day window is a day.
    expect(await screen.findByRole("img", { name: /today, 5-minute bars, 2 bars, up/i })).toBeInTheDocument();
    expect(seen.find((url) => url.includes("/symbols/AAPL/bars"))).toContain("interval=5m");
  });

  it("puts the window in the address, like the tab", async () => {
    mockApi();
    renderDES("AAPL");
    await screen.findByText("Apple Inc.");
    fireEvent.click(screen.getByRole("button", { name: "1Y" }));
    expect(screen.getByTestId("location").textContent).toBe("/ui/t/AAPL/DES?range=1y");
  });

  it("says a window has no bars rather than dropping the card", async () => {
    mockApi(INFO, []);
    renderDES("AAPL");
    expect(await screen.findByText("No bars for this window.")).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: /bars/ })).not.toBeInTheDocument();
  });

  it("draws the 52-week range and the analyst targets from the snapshot alone", async () => {
    mockApi({
      ...INFO,
      current_price: "245.5", fifty_two_week_low: "169.21", fifty_two_week_high: "260.1",
      two_hundred_day_average: "220.4",
      target_low_price: "200", target_mean_price: "260", target_high_price: "310",
    });
    renderDES("AAPL");
    expect(await screen.findByRole("img", { name: /52-week range, with the 200-day average/ })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /Analyst targets, low to high/ })).toBeInTheDocument();
  });

  it("says so when the symbol has never been synced", async () => {
    mockApi(null);
    renderDES("AAPL");
    expect(await screen.findByText(/Never synced: run yfin sync --symbols AAPL/)).toBeInTheDocument();
  });

  it("formats info values by what the key says they are", () => {
    expect(formatInfo("profit_margins", "0.2431")).toBe("24.31%");
    expect(formatInfo("dividend_yield", "0.34")).toBe("0.34%");
    // Yahoo is inconsistent inside one fund payload: a year-to-date
    // return arrives as a percentage and a three-year average as a
    // fraction. Measured, not assumed -- see the comment in `des/info.ts`.
    expect(formatInfo("ytd_return", "13.07293")).toBe("13.07%");
    expect(formatInfo("three_year_average_return", "0.2100781")).toBe("21.01%");
    expect(formatInfo("net_expense_ratio", "0.0945")).toBe("0.09%");
    expect(formatInfo("fund_yield", "0.0098")).toBe("0.98%");
    expect(formatInfo("regular_market_change_percent", "-2.5105848")).toBe("-2.51%");
    expect(formatInfo("ex_dividend_date", 1755043200)).toBe("2025-08-13");
    expect(formatInfo("regular_market_time", "1757016000")).toBe("2025-09-04 20:00 UTC");
    expect(formatInfo("first_trade_date", "1980-12-12T14:30:00Z")).toBe("1980-12-12 14:30 UTC");
    expect(formatInfo("last_fiscal_year_end", "2025-09-27T00:00:00Z")).toBe("2025-09-27");
    expect(formatInfo("market_cap", "4669700046848")).toBe("4.67T");
    expect(formatInfo("full_time_employees", 164000)).toBe("164,000");
    expect(formatInfo("regular_market_price", "1319.97")).toBe("1,319.97");
    expect(formatInfo("tradeable", true)).toBe("yes");
    expect(formatInfo("zip", "95014")).toBe("95014");
    expect(formatInfo("sector", "Technology")).toBe("Technology");
  });

  it("group places every non-null key somewhere and counts the nulls", () => {
    const info = {
      symbol: "AAPL", sector: "Technology", market_cap: "1", long_business_summary: "text",
      mystery: 1, other_mystery: null, beta: null,
    };
    const grouped = group(info);
    const keys = grouped.sections.flatMap(([, entries]) => entries.map(([k]) => k));
    expect(keys.sort()).toEqual(["market_cap", "mystery", "sector"]);
    expect(grouped.sections.map(([title]) => title)).toEqual(["Identity", "Valuation", "Other"]);
    expect(grouped.summary).toBe("text");
    expect(grouped.nulls).toBe(2);
  });

  it("takes a tab and a window in either order, and drops what a hand-edited link got wrong", () => {
    expect(DES_PANEL.parseArgs([])).toEqual({});
    expect(DES_PANEL.parseArgs(["OWNERSHIP"])).toEqual({ tab: "ownership" });
    expect(DES_PANEL.parseArgs(["1y"])).toEqual({ range: "1y" });
    expect(DES_PANEL.parseArgs(["1y", "price"])).toEqual({ range: "1y", tab: "price" });
    expect(DES_PANEL.parseArgs(["price", "1w"])).toEqual({ tab: "price", range: "1w" });
    expect(() => DES_PANEL.parseArgs(["bogus"])).toThrow(/Usage: DES \[price\|fundamentals/);
    expect(() => DES_PANEL.parseArgs(["price", "price"])).toThrow(/Usage: DES/);
    expect(DES_PANEL.normalizeArgs?.({ tab: "bogus", range: "5y" })).toEqual({});
    expect(DES_PANEL.normalizeArgs?.({ tab: "price", range: "1d" })).toEqual({ tab: "price", range: "1d" });
  });

  it("says so when the symbol does not exist", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      new Response(JSON.stringify({ type: "not_found", title: "No such symbol" }), {
        status: 404, headers: { "content-type": "application/problem+json" },
      }),
    );
    renderDES("NOPE");
    expect(await screen.findByText(/No such symbol/)).toBeInTheDocument();
  });

  it("shows a retry on a server error", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      new Response("{}", { status: 500, headers: { "content-type": "application/problem+json" } }),
    );
    renderDES("AAPL");
    expect(await screen.findByRole("button", { name: /retry/i })).toBeInTheDocument();
  });
});
