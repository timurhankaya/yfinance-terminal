// HEAT against a stubbed API. The treemap's own geometry is
// `viz/Treemap.test.tsx`; what is asserted here is the panel's
// contract -- which rows become boxes, which windows a source offers,
// and where Enter goes.
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router";
import {
  HEAT,
  HEAT_PANEL,
  HEAT_USAGE,
  HeatPeriod,
  SCREEN_PERIODS,
  SECTOR_PERIODS,
  periodsFor,
} from "./HEAT";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const SECTORS = {
  data: [
    { domain_key: "technology", name: "Technology", domain_type: "sector", parent_key: null },
    { domain_key: "energy", name: "Energy", domain_type: "sector", parent_key: null },
  ],
  next_cursor: null,
};

const METRICS = {
  data: [
    {
      domain_key: "technology",
      as_of_date: "2026-09-08",
      market_cap: "20000000000000",
      reg_market_change_pct: "0.015",
      ytd_change_pct: "0.18",
      one_year_change_pct: "0.25",
      three_year_change_pct: "0.6",
      five_year_change_pct: "1.4",
    },
    {
      domain_key: "energy",
      as_of_date: "2026-09-08",
      market_cap: "5000000000000",
      reg_market_change_pct: "-0.025",
      ytd_change_pct: "-0.04",
      one_year_change_pct: "0.03",
      three_year_change_pct: "0.1",
      five_year_change_pct: "0.4",
    },
    // Yesterday's row for a domain already seen: newest wins, and this
    // must not become a second box.
    {
      domain_key: "technology",
      as_of_date: "2026-09-07",
      market_cap: "19000000000000",
      reg_market_change_pct: "0.002",
      ytd_change_pct: "0.17",
      one_year_change_pct: "0.24",
      three_year_change_pct: "0.59",
      five_year_change_pct: "1.39",
    },
    // An industry: it has metrics but is not in the sector taxonomy.
    {
      domain_key: "software-infrastructure",
      as_of_date: "2026-09-08",
      market_cap: "9000000000000",
      reg_market_change_pct: "0.011000000000000001",
      ytd_change_pct: "0.12",
      one_year_change_pct: "0.2",
      three_year_change_pct: "0.5",
      five_year_change_pct: "1.2",
    },
  ],
  next_cursor: null,
};

const ROSTER = {
  data: {
    screen: {
      screen_key: "day_gainers",
      title: "Day gainers",
      description: null,
      kind: "predefined",
      quote_type: "EQUITY",
      sort_field: "percentchange",
      sort_asc: false,
      as_of_date: "2026-09-08",
      fetched_at: "2026-09-08T20:05:00Z",
      total: 2,
      row_count: 2,
    },
    rows: [
      {
        rank_index: 0,
        symbol: "AAA",
        is_known: true,
        short_name: "A",
        currency: "USD",
        exchange: "NMS",
        market_state: "REGULAR",
        price: "10",
        change: "1",
        change_percent: "12",
        volume: 1,
        market_cap: "3000000000",
        trailing_pe: null,
        fifty_two_week_change_percent: "40",
      },
      {
        rank_index: 1,
        symbol: "BBB",
        is_known: true,
        short_name: "B",
        currency: "USD",
        exchange: "NMS",
        market_state: "REGULAR",
        price: "20",
        change: "1",
        change_percent: "8",
        volume: 1,
        market_cap: null,
        trailing_pe: null,
        fifty_two_week_change_percent: null,
      },
    ],
    offset: 0,
    truncated: false,
  },
  as_of: null,
};

//: Every URL the panel asked for, so a test can assert what it did NOT.
const asked: string[] = [];

function stubSectors() {
  asked.length = 0;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    asked.push(url);
    // Answered, and the panel must still not ask: `domains` is
    // symbol-scoped, so the real API refuses this with a 422 and the
    // sector map drew nothing at all until it stopped asking.
    if (url.includes("/datasets/domains")) return json(SECTORS);
    if (url.includes("/datasets/domain_metrics")) return json(METRICS);
    return json({ data: [], next_cursor: null });
  });
}

function draw(args: Record<string, string>) {
  return render(
    <MemoryRouter initialEntries={["/ui/m/HEAT"]}>
      <Routes>
        <Route path="/ui/m/:code" element={<HEAT symbol={null} args={args} />} />
        <Route path="*" element={<p>navigated</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("HEAT parseArgs", () => {
  it("takes nothing, a window, a screen, or both", () => {
    expect(HEAT_PANEL.parseArgs([])).toEqual({ period: HeatPeriod.Day });
    expect(HEAT_PANEL.parseArgs(["1y"])).toEqual({ period: HeatPeriod.Year });
    expect(HEAT_PANEL.parseArgs(["day_gainers"])).toEqual({
      screen: "day_gainers",
      period: HeatPeriod.Day,
    });
    expect(HEAT_PANEL.parseArgs(["day_gainers", "52w"])).toEqual({
      screen: "day_gainers",
      period: HeatPeriod.Week52,
    });
  });

  it("refuses a window the source cannot answer, and names the ones it can", () => {
    // A screen's quote snapshot has today's move and the 52-week change;
    // three years is a sector map's question.
    expect(() => HEAT_PANEL.parseArgs(["day_gainers", "3y"])).toThrow(/1d, 52w/);
    expect(() => HEAT_PANEL.parseArgs(["52w"])).toThrow(/1d, ytd, 1y, 3y, 5y/);
  });

  it("refuses two screens, two windows, or a third token", () => {
    expect(() => HEAT_PANEL.parseArgs(["a", "b"])).toThrow(HEAT_USAGE);
    expect(() => HEAT_PANEL.parseArgs(["1d", "1y"])).toThrow(HEAT_USAGE);
    expect(() => HEAT_PANEL.parseArgs(["a", "1d", "x"])).toThrow(HEAT_USAGE);
  });

  it("runs without a symbol", () => {
    expect(HEAT_PANEL.needsSymbol).toBe(false);
  });

  it("offers each source only the windows it holds", () => {
    expect(periodsFor(undefined)).toEqual(SECTOR_PERIODS);
    expect(periodsFor("day_gainers")).toEqual(SCREEN_PERIODS);
    expect(SCREEN_PERIODS).toEqual([HeatPeriod.Day, HeatPeriod.Week52]);
  });
});

describe("normalizeArgs", () => {
  const normalize = HEAT_PANEL.normalizeArgs;

  it("brings a hand-edited window back to one the source answers", () => {
    expect(normalize?.({ period: "3y", screen: "day_gainers" })).toEqual({
      screen: "day_gainers",
      period: HeatPeriod.Day,
    });
    expect(normalize?.({ period: "nonsense" })).toEqual({ period: HeatPeriod.Day });
  });

  it("drops anything else the URL carried", () => {
    expect(normalize?.({ period: "1y", junk: "x" })).toEqual({ period: HeatPeriod.Year });
  });
});

describe("the sector map", () => {
  it("draws the newest row per sector, and nothing that is not one", async () => {
    // Two sectors: yesterday's technology row must not become a third
    // box, and an industry with metrics but no sector row must not
    // become one either.
    stubSectors();
    draw({ period: HeatPeriod.Day });
    await waitFor(() => {
      expect(screen.getAllByRole("button", { name: /Technology|Energy/ })).toHaveLength(2);
    });
    expect(screen.getByRole("button", { name: /Technology/ })).toHaveAccessibleName(
      /20\.00T · \+1\.5%/,
    );
    expect(screen.getByText(/2 boxes · today · sectors/)).toBeInTheDocument();
    // The taxonomy read is gone: `domains` is symbol-scoped and the API
    // refuses an unfiltered scan of it, which is what had been breaking
    // this panel outright. The sector list is a constant now.
    expect(asked.filter((url) => url.includes("/datasets/domains"))).toEqual([]);
  });

  it("colours by the window the args name", async () => {
    stubSectors();
    draw({ period: HeatPeriod.FiveYear });
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Technology/ })).toHaveAccessibleName(
        /\+140\.0%/,
      );
    });
  });

  it("opens the sector's metrics on Enter", async () => {
    stubSectors();
    draw({ period: HeatPeriod.Day });
    const box = await screen.findByRole("button", { name: /Technology/ });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(await screen.findByText("navigated")).toBeInTheDocument();
  });

  it("offers the five windows a metrics row carries, and says so", async () => {
    stubSectors();
    draw({ period: HeatPeriod.Day });
    await screen.findByRole("button", { name: /Technology/ });
    expect(screen.getAllByRole("tab")).toHaveLength(SECTOR_PERIODS.length);
    expect(screen.getByText(/carries these five windows/)).toBeInTheDocument();
  });
});

describe("the screen map", () => {
  it("draws only members that have a market capitalisation", async () => {
    // Area is size; a member with no market cap has no area, and a box
    // of zero would be invisible rather than absent.
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => json(ROSTER));
    draw({ screen: "day_gainers", period: HeatPeriod.Day });
    await waitFor(() => {
      expect(screen.getAllByRole("button", { name: /AAA/ })).toHaveLength(1);
    });
    expect(screen.queryByRole("button", { name: /BBB/ })).not.toBeInTheDocument();
    expect(screen.getByText(/1 box · today · screen day_gainers/)).toBeInTheDocument();
  });

  it("opens the symbol on Enter", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => json(ROSTER));
    draw({ screen: "day_gainers", period: HeatPeriod.Day });
    const box = await screen.findByRole("button", { name: /AAA/ });
    fireEvent.click(box);
    expect(await screen.findByText("navigated")).toBeInTheDocument();
  });

  it("points at EQS when the screen does not exist", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      json({ type: "not_found", title: "No such screen" }, 404),
    );
    draw({ screen: "nope", period: HeatPeriod.Day });
    expect(await screen.findByText(/No such screen: nope/)).toBeInTheDocument();
    expect(screen.getByText("EQS")).toBeInTheDocument();
  });

  it("says so rather than drawing an empty box when nothing has a size", async () => {
    const empty = {
      ...ROSTER,
      data: { ...ROSTER.data, rows: [ROSTER.data.rows[1]] },
    };
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => json(empty));
    draw({ screen: "day_gainers", period: HeatPeriod.Day });
    expect(await screen.findByText(/no day_gainers row carries a market capitalisation/)).toBeInTheDocument();
  });
});
