import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router";
import { FA, FA_PANEL } from "./FA";
import { SessionProvider } from "../app/session";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

const me = { authenticated: true, expires_at: 1, live_enabled: false };

const INCOME_URL = "/v1/symbols/AAPL/financials?statement=income&freq=annual&limit=1000";
const BALANCE_URL = "/v1/symbols/AAPL/financials?statement=balance_sheet&freq=annual&limit=1000";

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
    <SessionProvider>
      <MemoryRouter initialEntries={[path]}>
        <LocationProbe />
        <FA symbol="AAPL" args={args} />
      </MemoryRouter>
    </SessionProvider>,
  );
}

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
    expect(FA_PANEL.layout).toBe("single");
    expect(FA_PANEL.needsSymbol).toBe(true);
  });
});

describe("FA", () => {
  it("pivots facts into one row per item and one column per period, newest first", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/ui/api/me") return json(200, me);
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
      if (url === "/ui/api/me") return json(200, me);
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
      if (url === "/ui/api/me") return json(200, me);
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
