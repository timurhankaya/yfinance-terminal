import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { resetCatalogCache, WireType } from "../api/client";
import { CAL_PANEL, CURATED, HDS_PANEL, tabbedPanel } from "./curated";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}


const CATALOG = [
  {
    name: "major_holders", family: "holders", scope: "holders:read", kind: "symbol", table: "t",
    sort_key: [], descending: true, filters: [], symbol_scoped: true, description: "Split.",
    columns: [{ name: "symbol", type: WireType.String, nullable: false }, { name: "insiders_pct_held", type: WireType.Decimal, nullable: true }],
  },
  {
    name: "economic_calendar", family: "fundamentals", scope: "fundamentals:read", kind: "market", table: "t",
    sort_key: [], descending: true, filters: ["region"], symbol_scoped: false, description: "Macro.",
    columns: [{ name: "region", type: WireType.String, nullable: false }, { name: "event_name", type: WireType.String, nullable: false }],
  },
];

function LocationProbe() {
  const { pathname, search } = useLocation();
  return <span data-testid="location">{pathname + search}</span>;
}

function mockApi(rows: Record<string, unknown>[], seen: string[] = []) {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    seen.push(url);
    if (url === "/ui/api/v1/datasets") return json(200, { data: CATALOG, next_cursor: null });
    if (url.startsWith("/ui/api/v1/datasets/")) return json(200, { data: rows, next_cursor: null });
    throw new Error(`unexpected ${url}`);
  });
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  resetCatalogCache();
});

describe("tabbedPanel", () => {
  it("parses a tab key, filters, or both, and rejects anything else", () => {
    expect(HDS_PANEL.parseArgs([])).toEqual({ tab: "major" });
    expect(HDS_PANEL.parseArgs(["INST"])).toEqual({ tab: "inst" });
    expect(CAL_PANEL.parseArgs(["economic", "region=US"])).toEqual({ tab: "economic", region: "US" });
    expect(CAL_PANEL.parseArgs(["region=US"])).toEqual({ tab: "earnings", region: "US" });
    expect(() => HDS_PANEL.parseArgs(["bogus"])).toThrow(/Usage: HDS \[major\|inst/);
    expect(() => tabbedPanel({ code: "X", title: "x", tabs: [] })).toThrow(/at least one tab/);
  });

  it("needs a symbol only when every tab does", () => {
    expect(HDS_PANEL.needsSymbol).toBe(true);
    expect(CAL_PANEL.needsSymbol).toBe(false);
    expect(HDS_PANEL.usage).toContain("HDS [major|inst|funds|roster|trades|activity]");
  });

  it("registers a code and a usage line for every curated family", () => {
    const codes = CURATED.map((p) => p.code);
    expect(codes).toEqual(["HDS", "ERN", "FUND", "CAL", "MKT", "DOM", "REF"]);
    for (const panel of CURATED) expect(panel.usage).toContain(panel.code);
  });

  it("renders the tab's dataset for the strip symbol and switches tabs through the URL", async () => {
    const seen: string[] = [];
    mockApi([{ symbol: "AAPL", insiders_pct_held: "0.5" }], seen);
    const Component = HDS_PANEL.component;
    render(
      <MemoryRouter initialEntries={["/ui/t/AAPL/HDS"]}>
        <LocationProbe />
        <Component symbol="AAPL" args={{ tab: "major" }} />
      </MemoryRouter>,
    );
    expect(await screen.findByText("0.5000")).toBeInTheDocument();
    expect(seen.some((u) => u.includes("/datasets/major_holders?") && u.includes("symbol=AAPL"))).toBe(true);
    expect(screen.getByRole("tab", { name: "Major" }).getAttribute("aria-selected")).toBe("true");
    fireEvent.click(screen.getByRole("tab", { name: "Insider transactions" }));
    expect(screen.getByTestId("location").textContent).toBe("/ui/t/AAPL/HDS?tab=trades");
  });

  it("says a required tab needs a symbol instead of asking the API", async () => {
    const seen: string[] = [];
    mockApi([], seen);
    const Component = HDS_PANEL.component;
    render(
      <MemoryRouter initialEntries={["/ui/t/-/HDS"]}>
        <Component symbol={null} args={{ tab: "major" }} />
      </MemoryRouter>,
    );
    expect(await screen.findByText(/type one first, e.g. AAPL HDS major/)).toBeInTheDocument();
    expect(seen.filter((u) => u.includes("/datasets/"))).toEqual([]);
  });

  it("never sends the symbol to a market-wide tab, and forwards filters", async () => {
    const seen: string[] = [];
    mockApi([{ region: "US", event_name: "CPI" }], seen);
    const Component = CAL_PANEL.component;
    render(
      <MemoryRouter initialEntries={["/ui/t/AAPL/CAL"]}>
        <Component symbol="AAPL" args={{ tab: "economic", region: "US" }} />
      </MemoryRouter>,
    );
    expect(await screen.findByText("CPI")).toBeInTheDocument();
    const url = seen.find((u) => u.includes("/datasets/economic_calendar?"))!;
    expect(url).toContain("region=US");
    expect(url).not.toContain("symbol=");
  });
});
