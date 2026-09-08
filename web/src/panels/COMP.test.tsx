// COMP against a stubbed API, with the chart mocked: it draws to a
// canvas and jsdom has none. What is asserted is the contract between
// the panel and the chart -- which series it hands over, in what colour
// and what order -- plus what it says about the ones it could not draw.
// The normalisation itself is `chart-data.test.ts`.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router";
import { COMP, COMP_MAX, COMP_PANEL, COMP_USAGE, CompPeriod } from "./COMP";
import type { LineChartProps } from "./Chart";
import { Group, groupColor } from "./viz";

const drawn: LineChartProps[] = [];

vi.mock("./Chart", () => ({
  LineChart: (props: LineChartProps) => {
    drawn.push(props);
    return <div data-testid="chart" aria-label={props.label} />;
  },
}));

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function bar(date: string, close: string) {
  return { symbol: "X", ts_utc: date, open: close, high: close, low: close, close, volume: 1 };
}

/** A rising or falling two-session window per symbol, keyed by the URL
 *  the panel asks for. */
function stub(closes: Record<string, string[] | number>) {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    const symbol = /symbols\/([^/]+)\/bars/.exec(url)?.[1] ?? "";
    const found = closes[decodeURIComponent(symbol)];
    if (found === undefined) return json({ data: [], next_cursor: null });
    if (typeof found === "number") return json({ type: "not_found", title: "No such symbol" }, found);
    return json({
      data: found.map((close, index) => bar(`2026-09-0${index + 1}T00:00:00Z`, close)),
      next_cursor: null,
    });
  });
}

function draw(args: Record<string, string>) {
  return render(
    <MemoryRouter initialEntries={["/ui/m/COMP"]}>
      <Routes>
        <Route path="/ui/m/:code" element={<COMP symbol={null} args={args} />} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  drawn.length = 0;
  vi.restoreAllMocks();
});

describe("COMP parseArgs", () => {
  it("takes symbols and an optional window", () => {
    expect(COMP_PANEL.parseArgs(["aapl", "msft"])).toEqual({
      symbols: "AAPL,MSFT",
      period: CompPeriod.Year,
    });
    expect(COMP_PANEL.parseArgs(["aapl", "msft", "3y"])).toEqual({
      symbols: "AAPL,MSFT",
      period: CompPeriod.ThreeYear,
    });
  });

  it("refuses an eighth symbol rather than drawing two in one colour", () => {
    const eight = ["A", "B", "C", "D", "E", "F", "G", "H"];
    expect(eight).toHaveLength(COMP_MAX + 1);
    expect(() => COMP_PANEL.parseArgs(eight)).toThrow(COMP_USAGE);
  });

  it("refuses a token that is not a symbol, and a bare window", () => {
    expect(() => COMP_PANEL.parseArgs(["aapl", "not a symbol"])).toThrow(/is not a symbol/);
    expect(() => COMP_PANEL.parseArgs(["1y"])).toThrow(COMP_USAGE);
    expect(() => COMP_PANEL.parseArgs([])).toThrow(COMP_USAGE);
  });

  it("is a market page: no symbol, and single rather than headed", () => {
    // `headed` opens a live subscription for the strip's symbol, and a
    // comparison has no one symbol -- a shared link would carry
    // whichever happened to be up.
    expect(COMP_PANEL.needsSymbol).toBe(false);
    expect(COMP_PANEL.layout).toBe("single");
  });
});

describe("normalizeArgs", () => {
  it("keeps an over-long list for the panel to cut and report", () => {
    // Unlike `WLA`, whose cap is the socket's; this one is a limit on
    // what a reader can tell apart, so the panel says it applied it.
    const args = COMP_PANEL.normalizeArgs?.({ symbols: "A,B,C,D,E,F,G,H" });
    expect(args?.symbols).toBe("A,B,C,D,E,F,G,H");
  });

  it("drops junk tokens and fixes a hand-edited window", () => {
    expect(COMP_PANEL.normalizeArgs?.({ symbols: "AAPL,,not a symbol", period: "9y" })).toEqual({
      symbols: "AAPL",
      period: CompPeriod.Year,
    });
  });
});

describe("the comparison", () => {
  it("hands the chart one series per symbol, in argument order and palette order", async () => {
    stub({ AAPL: ["100", "110"], MSFT: ["50", "45"] });
    draw({ symbols: "AAPL,MSFT", period: CompPeriod.Year });
    await waitFor(() => expect(drawn.length).toBeGreaterThan(0));
    const last = drawn[drawn.length - 1];
    expect(last?.series.map((s) => s.key)).toEqual(["AAPL", "MSFT"]);
    expect(last?.series.map((s) => s.colour)).toEqual([
      groupColor(Group.A),
      groupColor(Group.B),
    ]);
    // Indexed to 100 at each series' own first session.
    expect(last?.series[0]?.points.map((p) => p.value)).toEqual([100, 110]);
    expect(last?.baseline).toBe(100);
  });

  it("names each series' change and the session it was indexed on", async () => {
    stub({ AAPL: ["100", "110"], MSFT: ["50", "45"] });
    draw({ symbols: "AAPL,MSFT", period: CompPeriod.Year });
    expect(await screen.findByText("+10%")).toBeInTheDocument();
    expect(screen.getByText("-10%")).toBeInTheDocument();
    expect(screen.getAllByText("from 2026-09-01")).toHaveLength(2);
  });

  it("lists a symbol with no bars instead of drawing it", async () => {
    // The colours are assigned by position in what is DRAWN, so a
    // missing symbol must not leave a hole in the palette.
    stub({ AAPL: ["100", "110"], ZZZZ: 404, MSFT: ["50", "45"] });
    draw({ symbols: "AAPL,ZZZZ,MSFT", period: CompPeriod.Year });
    expect(await screen.findByText(/No bars in this window: ZZZZ/)).toBeInTheDocument();
    const last = drawn[drawn.length - 1];
    expect(last?.series.map((s) => s.key)).toEqual(["AAPL", "MSFT"]);
    expect(last?.series[1]?.colour).toBe(groupColor(Group.B));
  });

  it("treats a one-session window as no series at all", async () => {
    // One point is not a shape: drawn alone it would be a dot at 100,
    // which reads as a symbol that went nowhere.
    stub({ AAPL: ["100"] });
    draw({ symbols: "AAPL", period: CompPeriod.Year });
    expect(await screen.findByText(/No daily bars for AAPL/)).toBeInTheDocument();
  });

  it("cuts an over-long list from the URL and says it did", async () => {
    stub({
      A: ["1", "2"], B: ["1", "2"], C: ["1", "2"], D: ["1", "2"],
      E: ["1", "2"], F: ["1", "2"], G: ["1", "2"], H: ["1", "2"],
    });
    draw({ symbols: "A,B,C,D,E,F,G,H", period: CompPeriod.Year });
    expect(await screen.findByText(/1 more symbol was left out/)).toBeInTheDocument();
    expect(drawn[drawn.length - 1]?.series).toHaveLength(COMP_MAX);
  });

  it("asks for the window the args name", async () => {
    const asked: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      asked.push(String(input));
      return json({ data: [bar("2026-09-01T00:00:00Z", "1"), bar("2026-09-02T00:00:00Z", "2")], next_cursor: null });
    });
    draw({ symbols: "AAPL", period: CompPeriod.SixMonth });
    await waitFor(() => expect(drawn.length).toBeGreaterThan(0));
    expect(asked[0]).toContain("interval=1d");
    const from = new URL(asked[0] ?? "", "http://x").searchParams.get("from") ?? "";
    const days = (Date.now() - Date.parse(from)) / 86_400_000;
    expect(days).toBeGreaterThan(180);
    expect(days).toBeLessThan(190);
  });

  it("says what to type when there is nothing to compare", () => {
    stub({});
    draw({});
    expect(screen.getByText(/Nothing to compare yet/)).toBeInTheDocument();
  });

  it("reports a failure that is not a missing symbol, with a retry", async () => {
    stub({ AAPL: 500 });
    draw({ symbols: "AAPL", period: CompPeriod.Year });
    expect(await screen.findByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});
