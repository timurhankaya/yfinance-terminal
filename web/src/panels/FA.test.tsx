import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router";
import { Layout } from "../commands/types";
import { FA, FA_PANEL, Freq, cell, incomeChart } from "./FA";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}


const INCOME_URL = "/ui/api/v1/symbols/AAPL/financials?statement=income&freq=annual&limit=1000";
const BALANCE_URL = "/ui/api/v1/symbols/AAPL/financials?statement=balance_sheet&freq=annual&limit=1000";

const incomeRows = [
  { period_end: "2025-09-30", item_key: "TotalRevenue", value: "391035000000", currency: "USD" },
  { period_end: "2024-09-30", item_key: "TotalRevenue", value: "383285000000", currency: "USD" },
  { period_end: "2025-09-30", item_key: "NetIncome", value: "93736000000", currency: "USD" },
];

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

// Shows where the router is, so tab clicks can be asserted on the URL.
function LocationProbe() {
  const { pathname, search } = useLocation();
  return <span data-testid="location">{pathname + search}</span>;
}

function renderFA(args: Record<string, string> = {}, path = "/ui/t/AAPL/FA") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <LocationProbe />
      <FA symbol="AAPL" args={args} />
    </MemoryRouter>,
  );
}

describe("cell", () => {
  it("keeps two decimals below a thousand and scales above it", () => {
    expect(cell("8.76")).toBe("8.76");
    expect(cell("0.173027")).toBe("0.17");
    expect(cell("-321000000")).toBe("-321.00M");
    expect(cell("391035000000")).toBe("391.04B");
    expect(cell(null)).toBe("—");
  });
});

describe("FA_PANEL.parseArgs", () => {
  it.each([
    [[], { statement: "income", freq: "annual" }],
    [["balance", "quarterly"], { statement: "balance_sheet", freq: "quarterly" }],
    [["BALANCE"], { statement: "balance_sheet", freq: "annual" }],
    [["cash", "TTM"], { statement: "cash_flow", freq: "ttm" }],
  ])("%j", (tokens, expected) => {
    expect(FA_PANEL.parseArgs(tokens)).toEqual(expected);
  });

  it("rejects an unknown statement or frequency with a usage line", () => {
    expect(() => FA_PANEL.parseArgs(["x"])).toThrow("Usage: FA [income|balance|cash] [annual|quarterly|ttm]");
    expect(() => FA_PANEL.parseArgs(["income", "monthly"])).toThrow("Usage: FA");
  });

  it("is registered as a single-layout panel that needs a symbol", () => {
    expect(FA_PANEL.code).toBe("FA");
    expect(FA_PANEL.layout).toBe(Layout.Single);
    expect(FA_PANEL.needsSymbol).toBe(true);
  });

  it("publishes its argument syntax so HELP can show it", () => {
    // The two most argument-rich panels were the two whose arguments
    // were undiscoverable.
    expect(FA_PANEL.usage).toBe("FA [income|balance|cash] [annual|quarterly|ttm]");
  });

  it("brings a hand-edited URL back to a statement the API serves", () => {
    expect(FA_PANEL.normalizeArgs?.({ statement: "nonsense", freq: "monthly" })).toEqual({
      statement: "income",
      freq: "annual",
    });
    expect(FA_PANEL.normalizeArgs?.({})).toEqual({ statement: "income", freq: "annual" });
    expect(FA_PANEL.normalizeArgs?.({ statement: "cash_flow", freq: "ttm" })).toEqual({
      statement: "cash_flow",
      freq: "ttm",
    });
  });
});

describe("FA", () => {
  it("pivots facts into one row per item and one column per period, newest first", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === INCOME_URL) return json(200, { data: incomeRows, next_cursor: null, as_of: null });
      throw new Error(`unexpected ${url}`);
    });
    renderFA();
    expect(await screen.findByText("TotalRevenue")).toBeInTheDocument();
    const headers = screen.getAllByRole("columnheader").map((h) => h.textContent);
    expect(headers).toEqual(["Item", "2025-09-30", "2024-09-30"]);
    expect(screen.getByText("391.04B")).toBeInTheDocument();
    expect(screen.getByText("383.29B")).toBeInTheDocument();
    // NetIncome has no 2024 value: the cell shows a dash, not an empty string.
    const netIncomeRow = screen.getByText("NetIncome").closest("tr");
    expect(netIncomeRow?.textContent).toContain("93.74B");
    expect(netIncomeRow?.textContent).toContain("—");
    expect(screen.getByText(/USD/)).toBeInTheDocument();
  });

  it("switches statement through the tabs by rewriting the URL args", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === INCOME_URL) return json(200, { data: incomeRows, next_cursor: null, as_of: null });
      if (url === BALANCE_URL) {
        return json(200, {
          data: [{ period_end: "2025-09-30", item_key: "TotalAssets", value: "364980000000", currency: "USD" }],
          next_cursor: null, as_of: null,
        });
      }
      throw new Error(`unexpected ${url}`);
    });
    renderFA();
    await screen.findByText("TotalRevenue");
    await userEvent.click(screen.getByRole("tab", { name: "Balance sheet" }));
    await waitFor(() =>
      expect(screen.getByTestId("location").textContent).toBe("/ui/t/AAPL/FA?statement=balance_sheet&freq=annual"),
    );
  });

  it("loads the statement and frequency named in the args", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === BALANCE_URL.replace("freq=annual", "freq=quarterly")) {
        return json(200, { data: [], next_cursor: null, as_of: null });
      }
      throw new Error(`unexpected ${url}`);
    });
    renderFA({ statement: "balance_sheet", freq: "quarterly" });
    expect(await screen.findByText("No financial statements for this symbol.")).toBeInTheDocument();
    expect(spy.mock.calls.some(([u]) => String(u).includes("statement=balance_sheet&freq=quarterly"))).toBe(true);
  });
});


describe("incomeChart", () => {
  const table = (periods: string[], rows: Array<{ item: string } & Record<string, unknown>>) => ({
    periods,
    rows,
    currency: "USD",
  });

  it("reads revenue and net income, oldest first", () => {
    // The table beside it is newest-first, because a table is read down;
    // an axis is time and is read left to right.
    const chart = incomeChart(
      table(
        ["2025-09-30", "2024-09-30"],
        [
          { item: "TotalRevenue", "2025-09-30": "400", "2024-09-30": "200" },
          { item: "NetIncome", "2025-09-30": "100", "2024-09-30": "40" },
        ],
      ),
      Freq.Annual,
    );
    expect(chart?.categories).toEqual(["2024", "2025"]);
    expect(chart?.revenue).toEqual([200, 400]);
    expect(chart?.income).toEqual([40, 100]);
    expect(chart?.margin).toEqual([20, 25]);
  });

  it("labels a quarter with its month, so two of a year can be told apart", () => {
    const chart = incomeChart(
      table(
        ["2026-06-30", "2026-03-31"],
        [
          { item: "TotalRevenue", "2026-06-30": "2", "2026-03-31": "1" },
          { item: "NetIncome", "2026-06-30": "1", "2026-03-31": "1" },
        ],
      ),
      Freq.Quarterly,
    );
    expect(chart?.categories).toEqual(["2026-03", "2026-06"]);
  });

  it("has no margin where revenue is missing or not positive", () => {
    const chart = incomeChart(
      table(
        ["2025-09-30", "2024-09-30"],
        [
          { item: "TotalRevenue", "2025-09-30": null, "2024-09-30": "0" },
          { item: "NetIncome", "2025-09-30": "100", "2024-09-30": "40" },
        ],
      ),
      Freq.Annual,
    );
    expect(chart?.margin).toEqual([null, null]);
  });

  it("is null on a statement with no revenue: half a chart is worse than none", () => {
    const balance = table(["2025-09-30"], [{ item: "TotalAssets", "2025-09-30": "1" }]);
    expect(incomeChart(balance, Freq.Annual)).toBeNull();
  });
});

describe("FA's chart", () => {
  it("draws revenue and net income above the table, on the income statement", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      json(200, { data: incomeRows, next_cursor: null }),
    );
    renderFA();
    const chart = await screen.findByRole("img", { name: /revenue and net income/ });
    const table = screen.getByRole("table");
    expect(chart.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("draws none on the balance sheet, which has neither", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      json(200, {
        data: [{ period_end: "2025-09-30", item_key: "TotalAssets", value: "1", currency: "USD" }],
        next_cursor: null,
      }),
    );
    renderFA({ statement: "balance_sheet", freq: "annual" });
    await screen.findByRole("table");
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });
});
