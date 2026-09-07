// GP, GIP and QR against a stubbed API.
//
// `lightweight-charts` is mocked: it draws to a canvas, and jsdom has no
// 2D context, so the real one throws on construction. What is asserted
// here is the contract between the panel and the chart -- which series
// it hands over, and what it says around them. The transforms themselves
// are `chart-data.test.ts`, where they are ordinary functions.
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ChartProps } from "./Chart";
import { GIP, GIP_PANEL, GIP_USAGE } from "./GIP";
import { GP, GP_PANEL, GP_USAGE } from "./GP";
import { QR, QR_PANEL, QR_USAGE, mergeTape, tapeClock } from "./QR";
import { MarketHours } from "../live/types";
import type { Tick } from "../live/types";
import { resetLive, setSocketFactory } from "../live/store";
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

describe("GP parseArgs", () => {
  it("takes a year count inside the archive's daily range", () => {
    expect(GP_PANEL.parseArgs([])).toEqual({ years: "2" });
    expect(GP_PANEL.parseArgs(["5"])).toEqual({ years: "5" });
    expect(() => GP_PANEL.parseArgs(["0"])).toThrow(GP_USAGE);
    expect(() => GP_PANEL.parseArgs(["11"])).toThrow(GP_USAGE);
    expect(() => GP_PANEL.parseArgs(["two"])).toThrow(GP_USAGE);
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

    render(<GP symbol="AAPL" args={{ years: "2" }} />);
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
    render(<GP symbol="ZZZZ" args={{}} />);
    expect(await screen.findByText(/No daily bars/)).toBeInTheDocument();
  });

  it("falls back to the default when the URL carries nonsense", async () => {
    // Args reach a panel from a hand-edited URL too, not only parseArgs.
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => page([bar("2026-09-03T13:30:00Z")]));
    render(<GP symbol="AAPL" args={{ years: "900" }} />);
    expect(await screen.findByText(/2 years/)).toBeInTheDocument();
  });
});

describe("GIP parseArgs", () => {
  it("takes an intraday interval and nothing else", () => {
    expect(GIP_PANEL.parseArgs([])).toEqual({ interval: "5m" });
    expect(GIP_PANEL.parseArgs(["1m"])).toEqual({ interval: "1m" });
    expect(() => GIP_PANEL.parseArgs(["1d"])).toThrow(GIP_USAGE);
    expect(() => GIP_PANEL.parseArgs(["15M"])).toThrow(GIP_USAGE);
  });
});

describe("GIP", () => {
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

    render(<GIP symbol="AAPL" args={{ interval: "5m" }} />);
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

  it("says so when the window is clean", async () => {
    // Silence would read as "no gaps here" and as "this panel does not
    // check" alike.
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) =>
      String(input).includes("/gaps") ? page([]) : page([bar("2026-09-08T13:30:00Z")]),
    );
    render(<GIP symbol="AAPL" args={{}} />);
    expect(await screen.findByText(/no open gaps/)).toBeInTheDocument();
  });
});

describe("QR", () => {
  it("rejects a row count above the route's ceiling", () => {
    expect(QR_PANEL.parseArgs([])).toEqual({});
    expect(QR_PANEL.parseArgs(["100"])).toEqual({ rows: "100" });
    expect(() => QR_PANEL.parseArgs(["2001"])).toThrow(QR_USAGE);
  });

  it("shows the archive's ticks newest first", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      page([tick({ t: 2_000, p: "2" }), tick({ t: 1_000, p: "1" })]),
    );
    render(<QR symbol="AAPL" args={{}} />);
    const list = await screen.findByRole("list", { name: /time and sales/ });
    expect(list.textContent).toContain("2.00");
    expect(screen.getByText(/2 ticks/)).toBeInTheDocument();
  });

  it("explains an empty tape instead of leaving a blank panel", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => page([]));
    render(<QR symbol="ZZZZ" args={{}} />);
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
});
