import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router";
import { EQS, EQS_PANEL, EQS_USAGE, countLabel, runLabel } from "./EQS";
import type { ScreenSummary } from "../api/client";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function summary(over: Partial<ScreenSummary> = {}): ScreenSummary {
  return {
    screen_key: "day_gainers",
    title: "Day gainers",
    description: "Up the most today.",
    kind: "predefined",
    quote_type: "EQUITY",
    sort_field: "percentchange",
    sort_asc: false,
    as_of_date: "2026-09-08",
    fetched_at: "2026-09-08T20:05:00Z",
    total: 120,
    row_count: 100,
    ...over,
  };
}

function row(over: Record<string, unknown> = {}) {
  return {
    rank_index: 0,
    symbol: "CCC",
    is_known: true,
    short_name: "Cee Corp",
    currency: "USD",
    exchange: "NMS",
    market_state: "REGULAR",
    price: "12.500000000000",
    change: "2.500000000000",
    change_percent: "25.000000000000",
    volume: 4_000_000,
    market_cap: "1250000000",
    trailing_pe: null,
    fifty_two_week_change_percent: null,
    ...over,
  };
}

/** The panel navigates, so it needs a router around it. */
function draw(args: Record<string, string>) {
  return render(
    <MemoryRouter initialEntries={["/ui/t/-/EQS"]}>
      <Routes>
        <Route path="/ui/t/:symbol/:code" element={<EQS symbol={null} args={args} />} />
        <Route path="*" element={<p>navigated</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("EQS parseArgs", () => {
  it("takes one screen name, or nothing", () => {
    expect(EQS_PANEL.parseArgs([])).toEqual({});
    expect(EQS_PANEL.parseArgs(["DAY_GAINERS"])).toEqual({ screen: "day_gainers" });
    expect(() => EQS_PANEL.parseArgs(["a", "b"])).toThrow(EQS_USAGE);
  });

  it("runs without a symbol", () => {
    expect(EQS_PANEL.needsSymbol).toBe(false);
  });
});

describe("the screen list", () => {
  it("shows each screen with when it ran and how many it matched", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      json({ data: [summary()], next_cursor: null }),
    );
    draw({});
    expect(await screen.findByText("day_gainers")).toBeInTheDocument();
    expect(screen.getByText(/120 matched, 100 kept/)).toBeInTheDocument();
    expect(screen.getByText(/2026-09-08 20:05 UTC/)).toBeInTheDocument();
  });

  it("says when nothing is enabled instead of leaving a blank panel", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      json({ data: [], next_cursor: null }),
    );
    draw({});
    expect(await screen.findByText(/None are enabled/)).toBeInTheDocument();
  });
});

describe("the roster", () => {
  it("keeps the screen's order and says what that order is", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      json({
        data: {
          screen: summary(),
          rows: [row(), row({ rank_index: 1, symbol: "AAA", short_name: "Aaa Inc" })],
          truncated: false,
        },
        as_of: "2026-09-08T20:05:00Z",
      }),
    );
    draw({ screen: "day_gainers" });
    const rows = await screen.findAllByRole("row");
    // Header, then rank 1, then rank 2 -- not alphabetical.
    expect(rows[1]!.textContent).toContain("CCC");
    expect(rows[2]!.textContent).toContain("AAA");
    expect(screen.getByText(/sorted by percentchange descending/)).toBeInTheDocument();
  });

  it("marks a member the archive has no quote for", async () => {
    // `screen_quotes` is outside the gate's delete scope, so a member can
    // exist without one; dropping it would shorten a roster whose length
    // is itself reported.
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      json({
        data: {
          screen: summary(),
          rows: [row({ symbol: "BBB", is_known: false, short_name: null, price: null })],
          truncated: false,
        },
        as_of: null,
      }),
    );
    draw({ screen: "day_gainers" });
    expect(await screen.findByText(/outside this deployment/)).toBeInTheDocument();
  });

  it("says the roster was cut rather than implying it ended", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      json({ data: { screen: summary(), rows: [row()], truncated: true }, as_of: null }),
    );
    draw({ screen: "day_gainers" });
    expect(await screen.findByText(/longer than this page shows/)).toBeInTheDocument();
  });

  it("tells the reader where the other ninety-odd columns are", async () => {
    // The terminal's rule is that nothing in the archive is unreachable;
    // a curated grid has to name the way to the rest.
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      json({ data: { screen: summary(), rows: [row()], truncated: false }, as_of: null }),
    );
    draw({ screen: "day_gainers" });
    expect(await screen.findByText(/DS screen_quotes/)).toBeInTheDocument();
  });

  it("reports an unknown screen as missing", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      json({ type: "not_found", title: "No such screen" }, 404),
    );
    draw({ screen: "nope" });
    expect(await screen.findByText(/No such symbol: nope/)).toBeInTheDocument();
  });
});

describe("labels", () => {
  it("distinguishes a screen that has never run from one with no matches", () => {
    expect(countLabel(summary({ total: null, row_count: null }))).toBe("no roster yet");
    expect(countLabel(summary({ total: 0, row_count: 0 }))).toBe("0 matched");
  });

  it("only reports a gap when the screen hit its page limit", () => {
    expect(countLabel(summary({ total: 100, row_count: 100 }))).toBe("100 matched");
  });

  it("falls back to the run date, then says it never ran", () => {
    expect(runLabel(summary({ fetched_at: null }))).toBe("last run 2026-09-08");
    expect(runLabel(summary({ fetched_at: null, as_of_date: null }))).toBe("never run");
  });
});
