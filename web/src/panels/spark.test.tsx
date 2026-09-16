// The sparkline column where it actually lands: one request for a whole
// page of rows, in three panels that write their tables three ways.
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router";
import { EQS } from "./EQS";
import { WLA } from "./WLA";
import { SPARK_FAILED_LABEL, SPARK_LABEL } from "./spark";
import { handleFrame, resetLive } from "../live/store";
import { MarketHours, Op } from "../live/types";
import type { SocketLike } from "../live/socket";

class DeadSocket implements SocketLike {
  onopen: ((event: unknown) => void) | null = null;
  onclose: ((event: unknown) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  send(): void {}
  close(): void {}
}

let frames: Array<() => void> = [];

function paint(): void {
  const queued = frames;
  frames = [];
  act(() => {
    for (const callback of queued) callback();
  });
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function closes(from: number, count: number): string[] {
  return Array.from({ length: count }, (_, index) => String(from + index));
}

function sparkSet(symbols: string[], missing: string[] = []) {
  return {
    data: {
      points: 30,
      series: symbols.map((symbol, index) => ({
        symbol,
        closes: closes(100 + index * 10, 30),
        first_date: "2026-07-28",
        last_date: "2026-09-08",
      })),
      missing,
    },
    as_of: null,
  };
}

const asked: string[] = [];

/** Records every URL and answers the sparkline batch; anything else is
 *  the caller's own body. */
function stub(rest: (url: string) => Response, sparks: () => Response) {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    asked.push(url);
    return url.includes("/sparklines") ? sparks() : rest(url);
  });
}

beforeEach(() => {
  asked.length = 0;
  frames = [];
  vi.stubGlobal("requestAnimationFrame", (callback: () => void) => {
    frames.push(callback);
    return frames.length;
  });
  vi.stubGlobal("cancelAnimationFrame", () => {});
  vi.stubGlobal("WebSocket", DeadSocket);
  resetLive();
});

afterEach(() => {
  cleanup();
  resetLive();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function drawWatchlist(symbols: string) {
  return render(
    <MemoryRouter initialEntries={["/ui/m/WLA"]}>
      <Routes>
        <Route path="/ui/m/:code" element={<WLA symbol={null} args={{ symbols }} />} />
        <Route path="*" element={<p>navigated</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("WLA", () => {
  it("asks for the whole list in one request", async () => {
    // The reason the route exists: 200 rows must not be 200 calls.
    stub(() => json({ data: [], next_cursor: null }), () => json(sparkSet(["AAPL", "MSFT", "NVDA"])));
    drawWatchlist("AAPL,MSFT,NVDA");
    await waitFor(() => {
      expect(screen.getAllByRole("img", { name: /sessions/ })).toHaveLength(3);
    });
    const sparkCalls = asked.filter((url) => url.includes("/sparklines"));
    expect(sparkCalls).toHaveLength(1);
    expect(sparkCalls[0]).toContain("symbols=AAPL%2CMSFT%2CNVDA");
    expect(sparkCalls[0]).toContain("points=30");
  });

  it("says a symbol has no bars rather than drawing a flat month", async () => {
    stub(() => json({ data: [], next_cursor: null }), () => json(sparkSet(["AAPL"], ["ZZZZ"])));
    drawWatchlist("AAPL,ZZZZ");
    expect(await screen.findByText("no data")).toBeInTheDocument();
    expect(screen.getAllByRole("img", { name: /sessions/ })).toHaveLength(1);
  });

  it("reports a failed batch once, in the heading, and leaves the rows alone", async () => {
    // The prices come from the socket; a dead REST call must not make a
    // live watchlist look broken.
    stub(
      () => json({ data: [], next_cursor: null }),
      () => json({ type: "internal", title: "boom" }, 500),
    );
    drawWatchlist("AAPL,MSFT");
    expect(await screen.findByText(SPARK_FAILED_LABEL)).toBeInTheDocument();
    expect(screen.queryByText(SPARK_LABEL)).not.toBeInTheDocument();
    expect(screen.getAllByRole("row")).toHaveLength(3); // head + two symbols
  });

  it("does not redraw a sparkline when the row ticks", async () => {
    // The other half of the memo assertion in `viz/Sparkline.test.tsx`:
    // a tick re-renders the row, and the shape it holds is a month of
    // closes that the tick cannot have changed.
    stub(() => json({ data: [], next_cursor: null }), () => json(sparkSet(["AAPL"])));
    drawWatchlist("AAPL");
    const before = await screen.findByRole("img", { name: /AAPL/ });
    const path = before.querySelector("path")?.getAttribute("d");

    act(() => {
      handleFrame({
        op: Op.Tick,
        d: { s: "AAPL", t: 1_000, p: "42", mh: MarketHours.Regular },
      });
    });
    paint();

    expect(screen.getByText("42.00")).toBeInTheDocument();
    const after = screen.getByRole("img", { name: /AAPL/ });
    expect(after).toBe(before); // the same node, not a redrawn one
    expect(after.querySelector("path")?.getAttribute("d")).toBe(path);
  });
});

describe("EQS", () => {
  const roster = {
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
        { rank_index: 0, symbol: "AAA", is_known: true, short_name: "A", currency: "USD", exchange: "NMS", market_state: "REGULAR", price: "1", change: "1", change_percent: "1", volume: 1, market_cap: "1", trailing_pe: null, fifty_two_week_change_percent: null },
        { rank_index: 1, symbol: "BBB", is_known: true, short_name: "B", currency: "USD", exchange: "NMS", market_state: "REGULAR", price: "2", change: "1", change_percent: "1", volume: 1, market_cap: "1", trailing_pe: null, fifty_two_week_change_percent: null },
      ],
      offset: 0,
      truncated: false,
    },
    as_of: null,
  };

  it("draws the column in its own Column[] and asks for the page's symbols", async () => {
    stub(() => json(roster), () => json(sparkSet(["AAA", "BBB"])));
    render(
      <MemoryRouter initialEntries={["/ui/m/EQS"]}>
        <Routes>
          <Route
            path="/ui/m/:code"
            element={<EQS symbol={null} args={{ screen: "day_gainers" }} />}
          />
        </Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByText(SPARK_LABEL)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getAllByRole("img", { name: /sessions/ })).toHaveLength(2);
    });
    const sparkCalls = asked.filter((url) => url.includes("/sparklines"));
    expect(sparkCalls).toHaveLength(1);
    expect(sparkCalls[0]).toContain("symbols=AAA%2CBBB");
  });
});
