// The strip says what it knows and, more importantly, what it does not.
// A price that has quietly stopped updating looks exactly like a quiet
// market, so each reason for stillness gets its own words.
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Strip, clock, direction } from "./Strip";
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

describe("Strip", () => {
  it("shows only the symbol under a single panel", () => {
    // A statement or a filing list must not hold a subscription for a
    // price nobody is looking at.
    render(<Strip symbol="AAPL" live={false} />);
    expect(screen.getByText("AAPL")).toBeInTheDocument();
    expect(screen.queryByText(/live stream off/)).not.toBeInTheDocument();
  });

  it("shows the price, the move and the session", () => {
    render(<Strip symbol="AAPL" live />);
    act(() => {
      handleFrame({ op: Op.Live, enabled: true });
      handleFrame({
        op: Op.Snap,
        d: tick({ c: "1.25", cp: "0.54", mh: MarketHours.PostMarket }),
      });
    });
    paint();
    expect(screen.getByText("232.35")).toBeInTheDocument();
    expect(screen.getByText(/1.25/)).toBeInTheDocument();
    expect(screen.getByText("after hours")).toBeInTheDocument();
  });

  it("says the stream is off rather than showing a still price", () => {
    render(<Strip symbol="AAPL" live />);
    act(() => {
      handleFrame({ op: Op.Live, enabled: false });
    });
    expect(screen.getByText(/live stream off/)).toBeInTheDocument();
  });

  it("distinguishes a symbol nobody streams from a broken socket", () => {
    render(<Strip symbol="ZZZZ" live />);
    act(() => {
      handleFrame({ op: Op.Live, enabled: true });
    });
    expect(screen.getByText(/no live price/)).toBeInTheDocument();
  });

  it("marks a lost socket while the stream itself is up", () => {
    render(<Strip symbol="AAPL" live />);
    act(() => {
      handleFrame({ op: Op.Live, enabled: true });
      useLive.setState({ link: LinkState.Closed });
    });
    expect(screen.getByLabelText("disconnected")).toBeInTheDocument();
  });

  it("counts what the server could not keep up with", () => {
    render(<Strip symbol="AAPL" live />);
    act(() => {
      handleFrame({ op: Op.Dropped, n: 7 });
    });
    expect(screen.getByText(/7 dropped/)).toBeInTheDocument();
  });
});

describe("clock", () => {
  it("is UTC, like the rest of the terminal", () => {
    expect(clock(1_788_877_815_250)).toBe("14:30 UTC");
  });
});

describe("direction", () => {
  it("colours a move but not a flat one", () => {
    // A grey zero is a fact; a green one is a story.
    expect(direction("1.25")).toBe("up");
    expect(direction("-1.25")).toBe("down");
    expect(direction("0")).toBeUndefined();
    expect(direction(undefined)).toBeUndefined();
  });
});
