import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DES, formatBig, formatInfo, group } from "./DES";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}


// vitest.config.ts sets `globals: false`, so @testing-library/react's
// automatic afterEach(cleanup) (which looks for a global `afterEach`)
// never registers; unmount explicitly so tests stay isolated.
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function renderDES(symbol: string) {
  return render(<DES symbol={symbol} args={{}} />);
}

describe("formatBig", () => {
  it("scales to K/M/B/T with two decimals", () => {
    expect(formatBig(3_500_000_000_000)).toBe("3.50T");
    expect(formatBig(12_345_678)).toBe("12.35M");
    expect(formatBig(999)).toBe("999");
  });
});

describe("DES", () => {
  it("renders identity and the info fields it knows", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.startsWith("/ui/api/v1/datasets/company_officers")) return json(200, { data: [], next_cursor: null });
      if (url === "/ui/api/v1/symbols/AAPL") return json(200, { data: {
        symbol: "AAPL", long_name: "Apple Inc.", short_name: "Apple", exchange: "NMS",
        full_exchange_name: "NasdaqGS", currency: "USD", quote_type: "EQUITY",
        timezone: "America/New_York", is_active: true,
        // Shaped like the real API: snake_case keys, Decimal numbers as
        // strings, dividend_yield already a percentage.
        info: {
          sector: "Technology", industry: "Consumer Electronics",
          market_cap: "4669700046848", trailing_pe: "36.609840000000",
          dividend_yield: "0.340000000000", website: "https://apple.com",
          beta: null, brand_new_key: "kept",
        },
      } });
      throw new Error(`unexpected ${url}`);
    });
    renderDES("AAPL");
    expect(await screen.findByText("Apple Inc.")).toBeInTheDocument();
    expect(screen.getByText("NasdaqGS")).toBeInTheDocument();
    expect(screen.getByText("Technology")).toBeInTheDocument();
    expect(screen.getByText("4.67T")).toBeInTheDocument();
    expect(screen.getByText("36.61")).toBeInTheDocument();
    expect(screen.getByText("0.34%")).toBeInTheDocument();
    // A null field is left out and counted; an unknown key lands in Other.
    expect(screen.queryByText("Beta")).not.toBeInTheDocument();
    expect(screen.getByText(/1 empty field/)).toBeInTheDocument();
    expect(screen.getByText("Other")).toBeInTheDocument();
    expect(screen.getByText("Brand new key")).toBeInTheDocument();
    expect(screen.getByText("kept")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "https://apple.com" })).toBeInTheDocument();
  });

  it("formats info values by what the key says they are", () => {
    expect(formatInfo("profit_margins", "0.2431")).toBe("24.31%");
    expect(formatInfo("dividend_yield", "0.34")).toBe("0.34%");
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
