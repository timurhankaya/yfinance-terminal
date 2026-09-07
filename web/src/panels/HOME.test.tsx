// The landing page is an index, not a second implementation: what it
// has to get right is that every card leads somewhere and that a cold
// archive says so rather than showing an empty frame.
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router";
import { HOME, HOME_PANEL } from "./HOME";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function screenRow(key: string) {
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
    total: 120,
    row_count: 100,
  };
}

function draw(symbol: string | null = null) {
  return render(
    <MemoryRouter initialEntries={["/ui"]}>
      <Routes>
        <Route path="/ui" element={<HOME symbol={symbol} args={{}} />} />
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

describe("HOME", () => {
  it("needs no symbol, so it sits on the market root", () => {
    expect(HOME_PANEL.needsSymbol).toBe(false);
  });

  it("says how to get to a symbol at all", async () => {
    // The page a first-time reader lands on has to teach the one thing
    // the terminal cannot infer: that you type a ticker.
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      json({ data: [], next_cursor: null }),
    );
    draw();
    expect(await screen.findByText(/Type a symbol to open its detail/)).toBeInTheDocument();
  });

  it("lists the screens that ran, with their counts", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) =>
      String(input).includes("/screens")
        ? json({ data: [screenRow("day_gainers")], next_cursor: null })
        : json({ data: [{ market_id: "us_market", status: "closed" }], next_cursor: null }),
    );
    draw();
    expect(await screen.findByText("day_gainers")).toBeInTheDocument();
    expect(screen.getByText(/120 matched, 100 kept/)).toBeInTheDocument();
    expect(screen.getByText("us_market")).toBeInTheDocument();
  });

  it("says a cold archive is cold rather than showing an empty frame", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      json({ data: [], next_cursor: null }),
    );
    draw();
    expect(await screen.findByText(/No market status in the archive yet/)).toBeInTheDocument();
    expect(screen.getByText(/No screens are enabled/)).toBeInTheDocument();
  });

  it("keeps working when the archive cannot be reached", async () => {
    // A landing page that throws is a terminal that looks broken before
    // the reader has typed anything.
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      json({ type: "internal", title: "boom" }, 500),
    );
    draw();
    expect(await screen.findByText(/Could not reach the archive/)).toBeInTheDocument();
    expect(screen.getByText(/Type a symbol to open its detail/)).toBeInTheDocument();
  });

  it("every card is a way into the panel that owns the data", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      json({ data: [], next_cursor: null }),
    );
    draw();
    await screen.findByText(/No screens are enabled/);
    for (const name of ["Markets", "Screens", "Calendars", "Everything"]) {
      expect(screen.getByRole("button", { name })).toBeInTheDocument();
    }
  });
});
