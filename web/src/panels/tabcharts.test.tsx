// The charts a curated tab declares: the transforms as ordinary
// functions, and the one thing the wiring has to get right -- that a
// tab which declares none draws none.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router";
import { HDS_PANEL } from "./curated";
import { TOP_HOLDERS, holderBars, insiderFlow } from "./tabcharts";
import type { Row } from "../api/client";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const HOLDER_COLUMNS = [
  { name: "symbol", type: "string", nullable: false },
  { name: "holder", type: "string", nullable: false },
  { name: "pct_held", type: "decimal", nullable: true },
];

const ACTIVITY_COLUMNS = [
  { name: "symbol", type: "string", nullable: false },
  { name: "period_label", type: "string", nullable: false },
  { name: "purchases_shares", type: "decimal", nullable: true },
  { name: "sales_shares", type: "decimal", nullable: true },
  { name: "net_shares", type: "decimal", nullable: true },
];

function entry(name: string, columns: unknown[]) {
  return {
    name,
    family: "holders",
    scope: "symbol",
    kind: "asof",
    table: name,
    sort_key: ["as_of_date"],
    descending: true,
    filters: [],
    symbol_scoped: true,
    description: name,
    columns,
  };
}

// The client caches the catalogue for the life of the module, so one
// catalogue carries every dataset the three tabs read.
const CATALOG = {
  data: [
    entry("institutional_holders", HOLDER_COLUMNS),
    entry("insider_purchases", ACTIVITY_COLUMNS),
    entry("major_holders", HOLDER_COLUMNS),
  ],
  next_cursor: null,
};

function drawTab(tab: string) {
  const Panel = HDS_PANEL.component;
  return render(
    <MemoryRouter initialEntries={["/ui/t/AAPL/HDS"]}>
      <Routes>
        <Route path="/ui/t/:symbol/:code" element={<Panel symbol="AAPL" args={{ tab }} />} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("holderBars", () => {
  const holder = (name: string, pct: string | null): Row => ({ holder: name, pct_held: pct });

  it("turns the archive's fraction into a percentage", () => {
    // `pct_held` is 0.0165 for 1.65% of the company.
    expect(holderBars([holder("Vanguard", "0.0165")])?.percents[0]).toBeCloseTo(1.65, 9);
  });

  it("takes the largest holders, biggest first", () => {
    const bars = holderBars([holder("Small", "0.01"), holder("Big", "0.09"), holder("Mid", "0.05")]);
    expect(bars?.holders).toEqual(["Big", "Mid", "Small"]);
  });

  it("stops at the top N, because a name has to fit under its bar", () => {
    const many = Array.from({ length: 20 }, (_, i) => holder(`Holder ${i}`, `0.0${i + 1}`));
    expect(holderBars(many)?.holders).toHaveLength(TOP_HOLDERS);
  });

  it("truncates a long name rather than letting two overlap", () => {
    const bars = holderBars([holder("Vanguard Group Incorporated", "0.09")]);
    expect(bars?.holders[0]).toBe("Vanguard Gr…");
  });

  it("is null when no row carries a share", () => {
    expect(holderBars([holder("Vanguard", null)])).toBeNull();
    expect(holderBars([])).toBeNull();
  });
});

describe("insiderFlow", () => {
  it("reads bought, sold and net off the newest snapshot", () => {
    const flow = insiderFlow([
      { period_label: "6m", purchases_shares: "100", sales_shares: "400", net_shares: "-300" },
      { period_label: "6m", purchases_shares: "1", sales_shares: "2", net_shares: "-1" },
    ]);
    expect(flow?.categories).toEqual(["Bought", "Sold", "Net"]);
    expect(flow?.shares).toEqual([100, 400, -300]);
    expect(flow?.period).toBe("6m");
  });

  it("keeps a negative net, which is the usual case", () => {
    // Signed on purpose in the schema; a bar below the baseline is the
    // answer, not a bug.
    expect(insiderFlow([{ net_shares: "-547806" }])?.shares[2]).toBe(-547806);
  });

  it("is null with no snapshot, or one carrying no share counts", () => {
    expect(insiderFlow([])).toBeNull();
    expect(insiderFlow([{ period_label: "6m" }])).toBeNull();
  });
});

describe("HDS", () => {
  it("draws the chart the tab declares, above its table", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/datasets")) return json(CATALOG);
      return json({
        data: [
          { symbol: "AAPL", holder: "Vanguard Group", pct_held: "0.09" },
          { symbol: "AAPL", holder: "Blackrock Inc", pct_held: "0.07" },
        ],
        next_cursor: null,
      });
    });
    drawTab("inst");
    const chart = await screen.findByRole("img", { name: /largest institutional holders/ });
    // Above the table: the picture never replaces the figures.
    const table = screen.getByRole("table");
    expect(chart.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("draws the insider flow on the tab that declares that one", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/datasets")) return json(CATALOG);
      return json({
        data: [
          {
            symbol: "AAPL",
            period_label: "6m",
            purchases_shares: "100",
            sales_shares: "400",
            net_shares: "-300",
          },
        ],
        next_cursor: null,
      });
    });
    drawTab("activity");
    expect(
      await screen.findByRole("img", { name: /insider shares bought and sold over 6m/ }),
    ).toBeInTheDocument();
  });

  it("draws none on a tab that declares none", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/datasets")) return json(CATALOG);
      return json({ data: [{ symbol: "AAPL", holder: "x", pct_held: "0.5" }], next_cursor: null });
    });
    drawTab("major");
    await waitFor(() => expect(screen.getByRole("table")).toBeInTheDocument());
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });
});
