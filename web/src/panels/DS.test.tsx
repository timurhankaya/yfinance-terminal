import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { resetCatalogCache, WireType } from "../api/client";
import { DS, DS_PANEL, DS_USAGE } from "./DS";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}


const CATALOG = [
  {
    name: "major_holders", family: "holders", scope: "holders:read", kind: "symbol", table: "holder_breakdown",
    sort_key: ["as_of_date"], descending: true, filters: [], symbol_scoped: true,
    description: "Ownership split.",
    columns: [
      { name: "symbol", type: WireType.String, nullable: false },
      { name: "as_of_date", type: WireType.Date, nullable: false },
      { name: "insiders_pct_held", type: WireType.Decimal, nullable: true },
    ],
  },
  {
    name: "market_status", family: "reference", scope: "reference:read", kind: "market", table: "market_status",
    sort_key: ["region"], descending: false, filters: ["region"], symbol_scoped: false,
    description: "Open or closed.",
    columns: [
      { name: "region", type: WireType.String, nullable: false },
      { name: "status", type: WireType.String, nullable: false },
    ],
  },
];

function LocationProbe() {
  const { pathname, search } = useLocation();
  return <span data-testid="location">{pathname + search}</span>;
}

function renderDS(symbol: string | null, args: Record<string, string>) {
  return render(
    <MemoryRouter initialEntries={["/ui/t/-/DS"]}>
      <LocationProbe />
      <DS symbol={symbol} args={args} />
    </MemoryRouter>,
  );
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

describe("DS parseArgs", () => {
  it("takes a dataset name and k=v filters", () => {
    expect(DS_PANEL.parseArgs([])).toEqual({});
    expect(DS_PANEL.parseArgs(["Market_Status", "region=US"])).toEqual({ name: "market_status", region: "US" });
    expect(() => DS_PANEL.parseArgs(["region=US"])).toThrow(DS_USAGE);
    expect(() => DS_PANEL.parseArgs(["market_status", "bogus"])).toThrow(DS_USAGE);
  });
});

describe("DS", () => {
  it("lists the catalogue grouped by family and opens a dataset on click", async () => {
    mockApi([]);
    renderDS("AAPL", {});
    const options = await screen.findAllByRole("option");
    expect(options).toHaveLength(2);
    expect(options[0]!.textContent).toContain("holders");
    expect(options[0]!.textContent).toContain("major_holders");
    expect(options[0]!.textContent).toContain("per symbol");
    expect(options[1]!.textContent).toContain("market-wide");
    expect(options[1]!.textContent).toContain("region=");
    fireEvent.click(options[1]!);
    expect(screen.getByTestId("location").textContent).toBe("/ui/t/AAPL/DS?name=market_status");
  });

  it("is reachable by Tab and names the active option", async () => {
    // The j/k/Enter model lives on a window listener; without a focusable
    // container and an active descendant it is invisible to a screen
    // reader and unreachable by keyboard alone.
    mockApi([]);
    renderDS("AAPL", {});
    const list = await screen.findByRole("listbox", { name: "datasets" });
    expect(list).toHaveAttribute("tabindex", "0");
    const options = screen.getAllByRole("option");
    expect(list.getAttribute("aria-activedescendant")).toBe(options[0]!.id);
    fireEvent.keyDown(window, { key: "j" });
    expect(list.getAttribute("aria-activedescendant")).toBe(options[1]!.id);
  });

  it("shows a symbol-scoped dataset filtered to the strip's symbol, symbol column hidden", async () => {
    const seen: string[] = [];
    mockApi([{ symbol: "AAPL", as_of_date: "2026-09-06", insiders_pct_held: "0.01648" }], seen);
    renderDS("AAPL", { name: "major_holders" });
    expect(await screen.findByText("0.0165")).toBeInTheDocument();
    expect(seen.some((u) => u.startsWith("/ui/api/v1/datasets/major_holders?") && u.includes("symbol=AAPL"))).toBe(true);
    const headers = screen.getAllByRole("columnheader").map((th) => th.textContent);
    // "open" carries the quote-page link every symbol row implies.
    expect(headers).toEqual(["open", "as_of_date", "insiders_pct_held"]);
  });

  it("passes filters through and never sends a symbol to a market-wide dataset", async () => {
    const seen: string[] = [];
    mockApi([{ region: "US", status: "closed" }], seen);
    renderDS("AAPL", { name: "market_status", region: "US" });
    expect(await screen.findByText("closed")).toBeInTheDocument();
    const url = seen.find((u) => u.startsWith("/ui/api/v1/datasets/market_status?"))!;
    expect(url).toContain("region=US");
    expect(url).not.toContain("symbol=");
  });

  it("loads the next page with the cursor and appends it", async () => {
    const seen: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      seen.push(url);
      if (url === "/ui/api/v1/datasets") return json(200, { data: CATALOG, next_cursor: null });
      if (url.includes("cursor=c1")) {
        return json(200, { data: [{ region: "GB", status: "open" }], next_cursor: null });
      }
      return json(200, { data: [{ region: "US", status: "closed" }], next_cursor: "c1" });
    });
    renderDS(null, { name: "market_status" });
    expect(await screen.findByText("closed")).toBeInTheDocument();
    expect(screen.getByText(/1 row loaded, more available/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByText("open")).toBeInTheDocument();
    expect(screen.getByText("closed")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
    const second = seen.find((u) => u.includes("cursor=c1"))!;
    expect(second).toContain("/ui/api/v1/datasets/market_status?");
    expect(second).toContain("limit=200");
  });

  it("names the accepted filters when the API refuses one", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/ui/api/v1/datasets") return json(200, { data: CATALOG, next_cursor: null });
      return new Response(JSON.stringify({ type: "invalid_parameter", title: "Unknown filter" }), {
        status: 422, headers: { "content-type": "application/problem+json" },
      });
    });
    renderDS("AAPL", { name: "market_status", bogus: "1" });
    expect(await screen.findByText(/Unknown filter; market_status accepts region=/)).toBeInTheDocument();
  });

  it("says when the name is not in the catalogue", async () => {
    mockApi([]);
    renderDS(null, { name: "nope" });
    expect(await screen.findByText(/No dataset named nope/)).toBeInTheDocument();
  });

  it("shows the API's refusal when a required symbol is missing", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/ui/api/v1/datasets") return json(200, { data: CATALOG, next_cursor: null });
      return new Response(JSON.stringify({ type: "invalid_parameter", title: "symbol is required" }), {
        status: 422, headers: { "content-type": "application/problem+json" },
      });
    });
    renderDS(null, { name: "major_holders" });
    expect(await screen.findByText(/symbol is required/)).toBeInTheDocument();
  });
});
