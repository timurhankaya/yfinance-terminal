import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router";
import { WLA, WLA_MAX, WLA_PANEL, WLA_USAGE, parseSymbols } from "./WLA";
import { handleFrame, resetLive, setSocketFactory, useLive } from "../live/store";
import type { SocketLike } from "../live/socket";
import { LinkState, MarketHours, Op } from "../live/types";
import type { Tick } from "../live/types";

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

function tick(over: Partial<Tick> = {}): Tick {
  return { s: "AAPL", t: 1_788_877_815_250, p: "232.35", mh: MarketHours.Regular, ...over };
}

function draw(args: Record<string, string>, symbol: string | null = null) {
  return render(
    <MemoryRouter initialEntries={["/ui/t/-/WLA"]}>
      <Routes>
        <Route path="/ui/t/:symbol/:code" element={<WLA symbol={symbol} args={args} />} />
        <Route path="*" element={<p>navigated</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  frames = [];
  vi.stubGlobal("requestAnimationFrame", (callback: () => void) => {
    frames.push(callback);
    return frames.length;
  });
  vi.stubGlobal("cancelAnimationFrame", () => {});
  setSocketFactory(() => new DeadSocket());
  resetLive();
});

afterEach(() => {
  cleanup();
  resetLive();
  setSocketFactory(null);
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("parseArgs", () => {
  it("takes a list of symbols", () => {
    expect(WLA_PANEL.parseArgs(["aapl", "msft"])).toEqual({ symbols: "AAPL,MSFT" });
    expect(WLA_PANEL.parseArgs([])).toEqual({});
  });

  it("names a token that is not a symbol rather than dropping it", () => {
    // A silently dropped token is a row the reader asked for and never
    // got, with nothing saying why.
    expect(() => WLA_PANEL.parseArgs(["AAPL", "not a symbol"])).toThrow(/not a symbol/);
  });

  it("refuses a list the socket would refuse", () => {
    const many = Array.from({ length: WLA_MAX + 1 }, (_, index) => `S${index}`);
    expect(() => WLA_PANEL.parseArgs(many)).toThrow(WLA_USAGE);
  });

  it("runs without a symbol on the strip", () => {
    expect(WLA_PANEL.needsSymbol).toBe(false);
  });
});

describe("parseSymbols", () => {
  it("drops duplicates, which would draw two rows that always agree", () => {
    expect(parseSymbols("AAPL,aapl,MSFT")).toEqual(["AAPL", "MSFT"]);
  });

  it("ignores junk in a hand-edited URL", () => {
    expect(parseSymbols("AAPL,,  ,../etc/passwd,MSFT")).toEqual(["AAPL", "MSFT"]);
  });

  it("caps the list at the socket's own ceiling", () => {
    const many = Array.from({ length: WLA_MAX + 50 }, (_, index) => `S${index}`).join(",");
    expect(parseSymbols(many)).toHaveLength(WLA_MAX);
  });

  it("is what normalizeArgs writes back into the URL", () => {
    expect(WLA_PANEL.normalizeArgs?.({ symbols: "aapl,aapl,junk!,msft" })).toEqual({
      symbols: "AAPL,MSFT",
    });
    expect(WLA_PANEL.normalizeArgs?.({ symbols: "!!!" })).toEqual({});
  });
});

describe("the grid", () => {
  it("shows a row per symbol, with the live price", () => {
    draw({ symbols: "AAPL,MSFT" });
    act(() => {
      handleFrame({ op: Op.Live, enabled: true });
      handleFrame({ op: Op.Snap, d: tick({ c: "1.25", cp: "0.54", v: 4_000_000 }) });
    });
    paint();
    const rows = screen.getAllByRole("row");
    expect(rows).toHaveLength(3); // header + two
    expect(rows[1]!.textContent).toContain("232.35");
    expect(rows[1]!.textContent).toContain("open");
  });

  it("says a symbol is not streamed rather than showing a blank price", () => {
    // A symbol outside `yfin stream scope` has no live path at all;
    // that is a configuration answer, not a missing one.
    draw({ symbols: "ZZZZ" });
    expect(screen.getByText("not streamed")).toBeInTheDocument();
  });

  it("watches the strip's symbol when the command carried no list", () => {
    draw({}, "AAPL");
    expect(screen.getByRole("row", { name: /AAPL/ })).toBeInTheDocument();
  });

  it("explains itself when there is nothing to watch at all", () => {
    draw({});
    expect(screen.getByText(/Nothing to watch yet/)).toBeInTheDocument();
    expect(screen.getByText(/the list is the/)).toBeInTheDocument();
  });

  it("says the stream is off rather than showing still prices", () => {
    draw({ symbols: "AAPL" });
    act(() => {
      handleFrame({ op: Op.Live, enabled: false });
    });
    expect(screen.getByText(/live stream off/)).toBeInTheDocument();
  });

  it("marks a lost socket while the stream itself is up", () => {
    draw({ symbols: "AAPL" });
    act(() => {
      handleFrame({ op: Op.Live, enabled: true });
      useLive.setState({ link: LinkState.Closed });
    });
    expect(screen.getByText("reconnecting")).toBeInTheDocument();
  });

  it("holds one row per symbol however long the list", () => {
    const many = Array.from({ length: WLA_MAX }, (_, index) => `S${index}`).join(",");
    draw({ symbols: many });
    // Header plus the cap: the panel refuses to draw more than the
    // socket would subscribe to.
    expect(screen.getAllByRole("row")).toHaveLength(WLA_MAX + 1);
  });
});
