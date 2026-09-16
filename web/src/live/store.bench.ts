// What the live store costs at watchlist size. Yahoo sends one snapshot
// per second per symbol, and the store coalesces a frame's worth, so the
// number that matters is the cost of ONE flush holding a whole round.
//
// Run it with:  npx vitest bench src/live/store.bench.ts
import { bench, describe } from "vitest";
import { handleFrame, resetLive, setTape } from "./store";
import { MarketHours, Op } from "./types";
import type { Tick } from "./types";

const SYMBOLS = (count: number): string[] =>
  Array.from({ length: count }, (_, index) => `SYM${index}`);

function tick(symbol: string, at: number): Tick {
  return { s: symbol, t: at, p: "232.35", mh: MarketHours.Regular, v: 1_000_000 };
}

/** One animation frame's worth of work: a full round of updates for
 *  every symbol, then the single flush that applies them. */
function round(symbols: string[], at: number, flush: () => void): void {
  for (const symbol of symbols) handleFrame({ op: Op.Tick, d: tick(symbol, at) });
  flush();
}

/** Collects the scheduled callback instead of waiting for a frame. */
function harness(): { flush: () => void; stop: () => void } {
  const queued: Array<() => void> = [];
  const realRaf = globalThis.requestAnimationFrame;
  const realCancel = globalThis.cancelAnimationFrame;
  globalThis.requestAnimationFrame = ((callback: () => void) => {
    queued.push(callback);
    return queued.length;
  }) as typeof globalThis.requestAnimationFrame;
  globalThis.cancelAnimationFrame = (() => {}) as typeof globalThis.cancelAnimationFrame;
  return {
    flush: () => {
      const pending = queued.splice(0, queued.length);
      for (const callback of pending) callback();
    },
    stop: () => {
      globalThis.requestAnimationFrame = realRaf;
      globalThis.cancelAnimationFrame = realCancel;
    },
  };
}

describe("one frame, by watchlist size", () => {
  for (const size of [1, 20, 100, 200]) {
    const symbols = SYMBOLS(size);
    let at = 1_000;
    const { flush } = harness();
    bench(`${size} symbols`, () => {
      at += 1_000;
      round(symbols, at, flush);
    });
  }
});

describe("the tape, which only one symbol has", () => {
  const symbols = SYMBOLS(200);
  let at = 1_000;
  const { flush } = harness();
  setTape("SYM0");
  bench("200 symbols, one of them taped", () => {
    at += 1_000;
    round(symbols, at, flush);
  });
});

describe("a burst inside one frame", () => {
  // What a market open looks like: several updates per symbol land
  // between two paints, and coalescing is what turns them into one
  // render.
  const symbols = SYMBOLS(200);
  let at = 1_000;
  const { flush } = harness();
  bench("200 symbols x 5 updates, one flush", () => {
    for (let repeat = 0; repeat < 5; repeat += 1) {
      at += 1;
      for (const symbol of symbols) handleFrame({ op: Op.Tick, d: tick(symbol, at) });
    }
    flush();
  });
});

// A bench file runs outside the suite, so nothing else resets this.
resetLive();
