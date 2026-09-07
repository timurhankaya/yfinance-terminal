// The running candle, under StrictMode.
//
// StrictMode is the point: React double-invokes updaters and effects
// there precisely to surface impurity, and this hook used to call
// `setRolledAt` from inside a `setBar` updater. What the tests pin is
// the contract the caller depends on -- one candle folded from the
// ticks, and `rolledAt` naming the newest bucket a tick opened, once,
// whatever React does to the render.
import { StrictMode } from "react";
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BucketMode } from "./chart-data";
import type { Candle } from "./chart-data";
import { useLiveSeries } from "./chart-live";
import { resetLive, setSocketFactory, useLive } from "../live/store";
import type { SocketLike } from "../live/socket";
import { MarketHours } from "../live/types";
import type { Tick } from "../live/types";

class DeadSocket implements SocketLike {
  onopen: ((event: unknown) => void) | null = null;
  onclose: ((event: unknown) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  send(): void {}
  close(): void {}
}

const STEP = 60;
//: One archived bar, opening the 07:00:00 minute bucket.
const BASE: Candle[] = [{ time: 25_200, open: 10, high: 12, low: 9, close: 11 }];

function tick(over: Partial<Tick> = {}): Tick {
  return { s: "AAPL", t: 25_200_000, p: "11.5", mh: MarketHours.Regular, ...over };
}

/** Puts a quote in the store the way a socket frame would, without one. */
function quote(over: Partial<Tick> = {}): void {
  act(() => {
    useLive.setState({ quotes: { AAPL: tick(over) } });
  });
}

function mount(base: Candle[] = BASE) {
  return renderHook(
    ({ candles }: { candles: Candle[] }) =>
      useLiveSeries(candles, "AAPL", STEP, BucketMode.Interval),
    { initialProps: { candles: base }, wrapper: StrictMode },
  );
}

beforeEach(() => {
  setSocketFactory(() => new DeadSocket());
  resetLive();
});

afterEach(() => {
  cleanup();
  resetLive();
  setSocketFactory(null);
  vi.restoreAllMocks();
});

describe("useLiveSeries", () => {
  it("folds a tick into the bar it belongs to rather than replacing it", () => {
    // The high has to stay honest: a bar that spiked to 13 and came back
    // keeps the 13.
    const { result } = mount();
    quote({ p: "13" });
    quote({ p: "11.2", t: 25_230_000 });
    expect(result.current.candles).toHaveLength(1);
    expect(result.current.candles[0]).toEqual({
      time: 25_200,
      open: 10,
      high: 13,
      low: 9,
      close: 11.2,
    });
    expect(result.current.rolledAt).toBeNull();
  });

  it("names the bucket a tick opened, once, and does not move backwards", () => {
    const { result } = mount();
    // 07:01:00 -- a bucket beyond the archive's last bar.
    quote({ p: "12", t: 25_260_000 });
    expect(result.current.rolledAt).toBe(25_260);
    expect(result.current.candles).toHaveLength(2);

    // Another tick inside the SAME new bucket extends it; the caller
    // must not read that as a second roll and refetch again.
    quote({ p: "12.5", t: 25_290_000 });
    expect(result.current.rolledAt).toBe(25_260);
    expect(result.current.candles).toHaveLength(2);
    expect(result.current.candles[1]?.close).toBe(12.5);
  });

  it("drops the running bar when a fresh REST load arrives", () => {
    // Those ticks are in the bars now; keeping the running candle would
    // draw one built from a stale open.
    const { result, rerender } = mount();
    quote({ p: "13" });
    expect(result.current.candles[0]?.high).toBe(13);

    // The reload says the bar actually opened at 20, not 10.
    const reloaded: Candle[] = [{ time: 25_200, open: 20, high: 20, low: 20, close: 20 }];
    rerender({ candles: reloaded });
    expect(result.current.candles).toHaveLength(1);
    // The still-current tick folds onto the FRESH bar: the open is the
    // archive's, not the one the discarded running candle carried.
    expect(result.current.candles[0]).toEqual({
      time: 25_200,
      open: 20,
      high: 20,
      low: 13,
      close: 13,
    });
  });

  it("leaves the chart alone for an extended-hours print", () => {
    // The series was asked for with `session=regular`; an extended print
    // would not match the bars under it.
    const { result } = mount();
    quote({ p: "99", mh: MarketHours.PostMarket, t: 25_260_000 });
    expect(result.current.candles).toBe(BASE);
    expect(result.current.rolledAt).toBeNull();
  });
});
