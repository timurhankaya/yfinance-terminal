// The home page is six independent reads. What it has to get right:
// every block leads somewhere, a cold or broken block says so on its own
// rather than blanking the page, and nothing here quietly shows a slice
// of the archive as though it were all of it.
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { HOME, HOME_PANEL } from "./index";
import { ahead } from "./Week";
import type { Row } from "../../api/client";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function page(rows: unknown[]) {
  return json({ data: rows, next_cursor: null });
}

function summary(symbol: string, name: string, percent: string) {
  return {
    symbol,
    short_name: name,
    regular_market_price: "100.000000",
    regular_market_change_percent: percent,
    market_state: "REGULAR",
  };
}

function screenSummary(key: string) {
  return {
    screen_key: key,
    title: key,
    description: null,
    kind: "predefined",
    quote_type: "EQUITY",
    sort_field: "percentchange",
    sort_asc: false,
    as_of_date: "2026-09-08",
    fetched_at: "2026-09-08T20:05:00Z",
    total: 10,
    row_count: 10,
  };
}

interface Stub {
  markets?: unknown[];
  news?: unknown[];
  screens?: unknown[];
  fail?: string;
}

function stub({ markets = [], news = [], screens = [], fail }: Stub) {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    if (fail !== undefined && url.includes(fail)) return json({ detail: "boom" }, 500);
    if (url.includes("/datasets/market_summary")) return page(markets);
    if (url.includes("/datasets/news")) return page(news);
    if (url.includes("/datasets/domains") || url.includes("/datasets/domain_metrics")) return page([]);
    if (url.includes("/datasets/calendar")) return page([]);
    if (url.includes("/ui/api/screens/")) return json({ data: { screen: screenSummary("x"), rows: [], offset: 0, truncated: false } });
    if (url.includes("/ui/api/screens")) return json({ data: screens });
    if (url.includes("/sparklines")) return json({ data: { points: 30, series: [], missing: [] } });
    throw new Error(`unexpected ${url}`);
  });
}

function LocationProbe() {
  const { pathname } = useLocation();
  return <span data-testid="location">{pathname}</span>;
}

function draw() {
  return render(
    <MemoryRouter initialEntries={["/ui"]}>
      <LocationProbe />
      <Routes>
        <Route path="/ui" element={<HOME symbol={null} args={{}} />} />
        <Route path="/ui/m/:code" element={<p>market page</p>} />
        <Route path="/ui/t/:symbol/:code" element={<p>symbol page</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("the home page", () => {
  it("needs no symbol, so it sits on the market root", () => {
    expect(HOME_PANEL.needsSymbol).toBe(false);
    expect(HOME_PANEL.code).toBe("HOME");
  });

  it("shows every market the archive quotes, not the first few", async () => {
    const markets = Array.from({ length: 25 }, (_, index) =>
      summary(`^IDX${String(index)}`, `Index ${String(index)}`, "1.5"),
    );
    stub({ markets });
    draw();
    const table = await screen.findByRole("table");
    // 25 rows and a heading row: the old page read a table that holds one.
    expect(within(table).getAllByRole("row")).toHaveLength(26);
  });

  it("opens a market's symbol from its row", async () => {
    stub({ markets: [summary("^GSPC", "S&P 500", "0.42")] });
    const user = userEvent.setup();
    draw();
    await user.click(await screen.findByText("^GSPC"));
    // `^` is encoded in a path segment, which is what makes the link safe.
    expect(screen.getByTestId("location").textContent).toBe("/ui/t/%5EGSPC/DES");
  });

  it("keeps the rest of the page when one block cannot read", async () => {
    stub({ markets: [summary("^GSPC", "S&P 500", "0.42")], fail: "/datasets/news" });
    draw();
    expect(await screen.findByText(/News: /)).toBeInTheDocument();
    // The failure is the block's, not the page's.
    expect(screen.getByText("^GSPC")).toBeInTheDocument();
  });

  it("says which screens exist when none of them is a movers screen", async () => {
    stub({ screens: [screenSummary("undervalued_growth")] });
    draw();
    expect(await screen.findByText(/No gainers or losers screen is enabled/)).toBeInTheDocument();
    expect(screen.getByText(/undervalued_growth/)).toBeInTheDocument();
  });

  it("tells the reader how to open a second panel", async () => {
    stub({});
    draw();
    expect(await screen.findByText(/pins a panel to a letter/)).toBeInTheDocument();
  });
});

describe("what is coming", () => {
  const now = Date.parse("2026-09-08T12:00:00Z");
  const rows = (when: string, values: string[]): Row[] =>
    values.map((value) => ({ [when]: value, symbol: value }) as Row);

  it("drops what has already happened and stops at ten days", () => {
    const result = ahead(
      [
        {
          rows: rows("event_start_ts_utc", [
            "2026-09-01T12:00:00Z",
            "2026-09-09T12:00:00Z",
            "2026-09-30T12:00:00Z",
          ]),
          full: false,
          kind: "earnings",
          when: "event_start_ts_utc",
          who: "symbol",
        },
      ],
      now,
    );
    expect(result.events.map((event) => event.who)).toEqual(["2026-09-09T12:00:00Z"]);
  });

  it("counts a day even when nothing happens on it", () => {
    const result = ahead(
      [
        {
          rows: rows("event_time_utc", ["2026-09-09T12:00:00Z", "2026-09-09T15:00:00Z"]),
          full: false,
          kind: "economic",
          when: "event_time_utc",
          who: "symbol",
        },
      ],
      now,
    );
    expect(result.perDay).toHaveLength(10);
    expect(result.perDay[1]).toEqual({ day: "2026-09-09", count: 2 });
    expect(result.perDay[2]?.count).toBe(0);
  });

  it("says when a calendar came back full, because then it is not all of it", () => {
    const result = ahead(
      [{ rows: [], full: true, kind: "splits", when: "payable_on_utc", who: "symbol" }],
      now,
    );
    expect(result.truncated).toEqual(["splits"]);
  });
});
