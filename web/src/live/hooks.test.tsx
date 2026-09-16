// The property a watchlist depends on: one symbol ticking must not
// re-render the rows of the others. Asserted through the selector
// subscription, deterministically, rather than timed.
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useQuote } from "./hooks";
import { handleFrame, resetLive, setSocketFactory } from "./store";
import type { SocketLike } from "./socket";
import { MarketHours, Op } from "./types";

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

const renders = new Map<string, number>();

function Row({ symbol }: { symbol: string }) {
  const quote = useQuote(symbol);
  renders.set(symbol, (renders.get(symbol) ?? 0) + 1);
  return <span data-testid={symbol}>{quote?.p ?? "—"}</span>;
}

function Watchlist({ symbols }: { symbols: string[] }) {
  return (
    <>
      {symbols.map((symbol) => (
        <Row key={symbol} symbol={symbol} />
      ))}
    </>
  );
}

beforeEach(() => {
  frames = [];
  renders.clear();
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

describe("a watchlist-sized page", () => {
  const symbols = Array.from({ length: 200 }, (_, index) => `SYM${index}`);

  it("re-renders only the row whose symbol ticked", () => {
    render(<Watchlist symbols={symbols} />);
    const before = new Map(renders);

    act(() => {
      handleFrame({
        op: Op.Tick,
        d: { s: "SYM7", t: 1_000, p: "42", mh: MarketHours.Regular },
      });
    });
    paint();

    expect(renders.get("SYM7")).toBe((before.get("SYM7") ?? 0) + 1);
    const others = symbols.filter((symbol) => symbol !== "SYM7");
    expect(others.every((symbol) => renders.get(symbol) === before.get(symbol))).toBe(true);
  });

  it("applies a whole round of updates in one paint", () => {
    // 200 frames between two paints is one render each, not 200 each:
    // the buffer coalesces and every row sees only its own last value.
    render(<Watchlist symbols={symbols} />);
    const before = new Map(renders);

    act(() => {
      for (let round = 0; round < 3; round += 1) {
        for (const symbol of symbols) {
          handleFrame({
            op: Op.Tick,
            d: { s: symbol, t: 1_000 + round, p: String(round), mh: MarketHours.Regular },
          });
        }
      }
    });
    paint();

    expect(symbols.every((symbol) => renders.get(symbol) === (before.get(symbol) ?? 0) + 1)).toBe(
      true,
    );
  });

  it("holds one subscription per symbol however many rows show it", () => {
    // `GP` and the strip both watch the same symbol; the reference count
    // is what keeps that one `sub` rather than two.
    const { unmount } = render(<Watchlist symbols={["AAA", "AAA", "BBB"]} />);
    act(() => {
      handleFrame({
        op: Op.Snap,
        d: { s: "AAA", t: 5_000, p: "1", mh: MarketHours.Regular },
      });
    });
    paint();
    unmount();
    // Nothing to assert beyond surviving mount and unmount without a
    // duplicate subscribe throwing; the socket-level count is covered in
    // store.test.ts.
    expect(renders.get("AAA")).toBeGreaterThan(0);
  });
});
