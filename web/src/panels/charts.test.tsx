// GP and QR against a stubbed API.
//
// `lightweight-charts` is mocked: it draws to a canvas, and jsdom has no
// 2D context, so the real one throws on construction. What is asserted
// here is the contract between the panel and the chart -- which series
// it hands over, and what it says around them. The transforms themselves
// are `chart-data.test.ts`, where they are ordinary functions.
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ChartProps } from "./Chart";
import { GP, GP_PANEL, GP_USAGE, isIntraday } from "./GP";
import { BAR_INTERVALS } from "../api/client";
import { QR, QR_PANEL, QR_USAGE, mergeTape, tapeClock } from "./QR";
import { LinkState, MarketHours } from "../live/types";
import type { Tick } from "../live/types";
import { resetLive, setSocketFactory, useLive } from "../live/store";
import type { SocketLike } from "../live/socket";

const drawn: ChartProps[] = [];

vi.mock("./Chart", () => ({
  Chart: (props: ChartProps) => {
    drawn.push(props);
    return <div data-testid="chart" aria-label={props.label} />;
  },
}));

class DeadSocket implements SocketLike {
  onopen: ((event: unknown) => void) | null = null;
  onclose: ((event: unknown) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  send(): void {}
  close(): void {}
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function page(data: unknown[]): Response {
  return json({ data, next_cursor: null });
}

function bar(date: string, over: Record<string, unknown> = {}) {
  return {
    symbol: "AAPL",
    ts_utc: date,
    open: "100",
    high: "110",
    low: "90",
    close: "105",
    volume: 1000,
    ...over,
  };
}

function tick(over: Partial<Tick> = {}): Tick {
  return { s: "AAPL", t: 1_788_877_815_250, p: "232.35", mh: MarketHours.Regular, ...over };
}

/** The last props the mocked chart was rendered with. */
function lastChart(): ChartProps {
  const props = drawn[drawn.length - 1];
  if (props === undefined) throw new Error("the chart was never rendered");
  return props;
}

beforeEach(() => {
  drawn.length = 0;
  // The panels retain a symbol through `useQuote`; without a transport
  // the store would try to open a real WebSocket.
  setSocketFactory(() => new DeadSocket());
});

afterEach(() => {
  cleanup();
  resetLive();
  setSocketFactory(null);
  vi.restoreAllMocks();
});

/** The chart panels carry interval and window controls now, and a
 *  control runs a command -- so they need the router the app always has
 *  around them. */
function draw(element: ReactElement) {
  return render(<MemoryRouter>{element}</MemoryRouter>);
}

describe("GP parseArgs", () => {
  it("takes an interval and, above intraday, a year count", () => {
    expect(GP_PANEL.parseArgs([])).toEqual({ interval: "1d", years: "2" });
    expect(GP_PANEL.parseArgs(["1d", "5"])).toEqual({ interval: "1d", years: "5" });
    expect(GP_PANEL.parseArgs(["1WK"])).toEqual({ interval: "1wk", years: "2" });
    expect(GP_PANEL.parseArgs(["5m"])).toEqual({ interval: "5m", years: "2" });
    expect(() => GP_PANEL.parseArgs(["1d", "0"])).toThrow(GP_USAGE);
    expect(() => GP_PANEL.parseArgs(["1d", "11"])).toThrow(GP_USAGE);
    expect(() => GP_PANEL.parseArgs(["nonsense"])).toThrow(GP_USAGE);
    // A window on an intraday interval is refused rather than ignored:
    // there is no such thing as three years of five-minute bars.
    expect(() => GP_PANEL.parseArgs(["5m", "3"])).toThrow(/years applies to/);
  });

  it("splits the whole interval vocabulary between its two bodies", () => {
    // The merge's one invariant: every interval the API serves is drawn
    // by exactly one of the two, so no interval reaches a blank panel.
    for (const interval of BAR_INTERVALS) {
      expect(typeof isIntraday(interval), interval).toBe("boolean");
      expect(GP_PANEL.parseArgs([interval]).interval).toBe(interval);
    }
    expect(BAR_INTERVALS.filter(isIntraday)).toEqual(["1m", "5m", "15m", "60m"]);
  });
});

describe("GP", () => {
  it("draws the daily bars and marks the actions", async () => {
    const seen: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      seen.push(url);
      if (url.includes("/actions")) {
        return page([
          { symbol: "AAPL", action_date: "2026-09-04", action_type: "DIVIDEND", action_value: "0.25" },
        ]);
      }
      return page([bar("2026-09-03T13:30:00Z"), bar("2026-09-04T13:30:00Z")]);
    });

    draw(<GP symbol="AAPL" args={{ interval: "1d", years: "2" }} />);
    await screen.findByTestId("chart");

    const props = lastChart();
    expect(props.candles.map((c) => c.close)).toEqual([105, 105]);
    expect(props.volume).toHaveLength(2);
    expect(props.markers.map((m) => m.text)).toEqual(["D 0.25"]);
    // A daily chart shows dates, and there are no gaps to shade.
    expect(props.timeVisible).toBe(false);
    expect(props.band).toEqual([]);
    expect(seen.some((url) => url.includes("interval=1d&from="))).toBe(true);
    // `session` is intraday-only; sending it above daily is a 422.
    expect(seen.some((url) => url.includes("/bars?") && url.includes("session="))).toBe(false);
  });

  it("says the archive has nothing rather than drawing an empty chart", async () => {
    // A fresh Response per call: a body can only be read once, and GP
    // asks for bars and actions at the same time.
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => page([]));
    draw(<GP symbol="ZZZZ" args={{}} />);
    expect(await screen.findByText(/No 1d bars/)).toBeInTheDocument();
  });

  it("falls back to the default when the URL carries nonsense", () => {
    // Args reach a panel from a hand-edited URL too, not only parseArgs,
    // and `normalizeArgs` is where that is settled -- once, before the
    // component runs, so the panel body has one contract instead of
    // re-validating every arg it reads.
    expect(GP_PANEL.normalizeArgs?.({ years: "900" })).toEqual({ interval: "1d", years: "2" });
    expect(GP_PANEL.normalizeArgs?.({ years: "zero" })).toEqual({ interval: "1d", years: "2" });
    expect(GP_PANEL.normalizeArgs?.({})).toEqual({ interval: "1d", years: "2" });
    expect(GP_PANEL.normalizeArgs?.({ interval: "5m" })).toEqual({ interval: "5m", years: "2" });
    // A real interval the archive does not serve, and a word that is not
    // one, both come back as the default rather than a blank chart.
    expect(GP_PANEL.normalizeArgs?.({ interval: "nonsense" })).toEqual({ interval: "1d", years: "2" });
  });
});

describe("GP at an intraday interval", () => {
  it("asks for the regular session and shades the archive's gaps", async () => {
    const seen: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      seen.push(url);
      if (url.includes("/gaps")) {
        return page([
          {
            bar_interval: "5m",
            gap_start_utc: "2026-09-08T13:35:00Z",
            gap_end_utc: "2026-09-08T13:50:00Z",
            reason: "fetch_failed",
            detected_at: "2026-09-08T14:00:00Z",
          },
        ]);
      }
      return page([bar("2026-09-08T13:30:00Z"), bar("2026-09-08T13:55:00Z")]);
    });

    draw(<GP symbol="AAPL" args={{ interval: "5m" }} />);
    await screen.findByTestId("chart");

    const props = lastChart();
    expect(props.timeVisible).toBe(true);
    // Three five-minute slots between 13:35 and 13:50.
    expect(props.whitespace).toHaveLength(3);
    expect(props.band).toEqual(props.whitespace);
    expect(seen.some((url) => url.includes("session=regular"))).toBe(true);
    expect(seen.some((url) => url.includes("/gaps?interval=5m&from="))).toBe(true);
    expect(screen.getByText(/1 open gap/)).toBeInTheDocument();
  });

  it("refreshes after a reconnect without unmounting the chart", async () => {
    // `Chart` builds its canvas once on purpose -- rebuilding it would
    // lose the reader's pan and zoom. A roll or a reconnect refetches the
    // SAME window, so that refresh must not pass through "Loading…".
    let barFetches = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      if (String(input).includes("/gaps")) return page([]);
      barFetches += 1;
      return page([bar("2026-09-08T13:30:00Z")]);
    });
    draw(<GP symbol="AAPL" args={{ interval: "5m" }} />);
    const node = await screen.findByTestId("chart");
    expect(barFetches).toBe(1);

    // The socket comes back after a drop: what arrived while it was down
    // was never delivered, so the archive is asked again.
    act(() => {
      useLive.setState({ link: LinkState.Open });
    });
    await waitFor(() => expect(barFetches).toBe(2));
    expect(screen.queryByText(/Loading/)).toBeNull();
    expect(screen.getByTestId("chart")).toBe(node);
  });

  it("says so when the window is clean", async () => {
    // Silence would read as "no gaps here" and as "this panel does not
    // check" alike.
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) =>
      String(input).includes("/gaps") ? page([]) : page([bar("2026-09-08T13:30:00Z")]),
    );
    draw(<GP symbol="AAPL" args={{ interval: "5m" }} />);
    expect(await screen.findByText(/no open gaps/)).toBeInTheDocument();
  });
});

describe("QR", () => {
  it("rejects a row count above the route's ceiling", () => {
    expect(QR_PANEL.parseArgs([])).toEqual({});
    expect(QR_PANEL.parseArgs(["100"])).toEqual({ rows: "100" });
    expect(() => QR_PANEL.parseArgs(["2001"])).toThrow(QR_USAGE);
  });

  it("clamps a hand-edited row count back to the default", () => {
    expect(QR_PANEL.normalizeArgs?.({ rows: "9999" })).toEqual({ rows: "500" });
    expect(QR_PANEL.normalizeArgs?.({ rows: "100" })).toEqual({ rows: "100" });
  });

  it("shows the archive's ticks newest first", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      page([tick({ t: 2_000, p: "2" }), tick({ t: 1_000, p: "1" })]),
    );
    draw(<QR symbol="AAPL" args={{}} />);
    const list = await screen.findByRole("list", { name: /time and sales/ });
    expect(list.textContent).toContain("2.00");
    expect(screen.getByText(/2 ticks/)).toBeInTheDocument();
  });

  it("explains an empty tape instead of leaving a blank panel", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => page([]));
    draw(<QR symbol="ZZZZ" args={{}} />);
    expect(await screen.findByText(/live stream is off/)).toBeInTheDocument();
  });

  it("shows the time in UTC to the second", () => {
    expect(tapeClock(1_788_877_815_250)).toBe("14:30:15");
  });
});

describe("mergeTape", () => {
  it("drops a tick that reached the page twice", () => {
    // The writer republishes rows that `ON CONFLICT DO NOTHING` dropped,
    // and the opening page overlaps whatever arrived while it was in
    // flight.
    const shared = tick({ t: 1_000 });
    const items = mergeTape([shared], [shared, tick({ t: 500 })], new Set());
    expect(items.map((item) => item.tick.t)).toEqual([1_000, 500]);
  });

  it("keeps the live rows above the archive's", () => {
    const items = mergeTape([tick({ t: 9_000 })], [tick({ t: 1_000 })], new Set());
    expect(items[0]?.tick.t).toBe(9_000);
  });

  it("marks where the socket dropped", () => {
    const items = mergeTape([tick({ t: 9_000 })], [], new Set(["AAPL|9000"]));
    expect(items[0]?.breakBefore).toBe(true);
  });

  it("knows which rows the socket delivered", () => {
    const items = mergeTape([tick({ t: 9_000 })], [tick({ t: 1_000 })], new Set());
    expect(items.map((item) => item.live)).toEqual([true, false]);
  });
});
