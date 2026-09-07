import { describe, expect, it } from "vitest";
import {
  BucketMode,
  GAP_SLOT_LIMIT,
  MarkerKind,
  applyTick,
  bucketOf,
  gapBands,
  toCandles,
  toMarkers,
  toVolume,
} from "./chart-data";
import type { Candle } from "./chart-data";
import { MarketHours } from "../live/types";
import type { Tick } from "../live/types";

const T0 = Date.parse("2026-09-08T13:30:00Z") / 1000;
const FIVE_MIN = 300;

function bar(offset: number, over: Record<string, unknown> = {}) {
  return {
    ts_utc: new Date((T0 + offset) * 1000).toISOString(),
    open: "100",
    high: "110",
    low: "90",
    close: "105",
    volume: 1000,
    ...over,
  };
}

function candle(time: number, over: Partial<Candle> = {}): Candle {
  return { time, open: 100, high: 110, low: 90, close: 105, ...over };
}

function tick(over: Partial<Tick> = {}): Tick {
  return { s: "AAPL", t: (T0 + 60) * 1000, p: "107", mh: MarketHours.Regular, ...over };
}

describe("toCandles", () => {
  it("keeps the archive's order whatever order the rows arrive in", () => {
    const candles = toCandles([bar(600), bar(0), bar(300)]);
    expect(candles.map((c) => c.time)).toEqual([T0, T0 + 300, T0 + 600]);
  });

  it("falls back to the close for the columns that are nullable", () => {
    // Only `close` is NOT NULL in the archive. A bar with a price in it
    // draws flat rather than not at all.
    const [only] = toCandles([bar(0, { open: null, high: null, low: null })]);
    expect(only).toEqual({ time: T0, open: 105, high: 105, low: 105, close: 105 });
  });

  it("drops a row with no close", () => {
    expect(toCandles([bar(0, { close: null })])).toEqual([]);
  });

  it("keeps one candle per instant", () => {
    // The chart library refuses a series whose times are not strictly
    // increasing, and a duplicate is what a page overlap looks like.
    const candles = toCandles([bar(0, { close: "1" }), bar(0, { close: "2" })]);
    expect(candles).toHaveLength(1);
    expect(candles[0]?.close).toBe(2);
  });
});

describe("toVolume", () => {
  it("colours each bar by the candle above it", () => {
    const rows = [bar(0, { open: "100", close: "105" }), bar(300, { open: "105", close: "100" })];
    const [up, down] = toVolume(toCandles(rows), rows);
    expect(up?.color).not.toBe(down?.color);
  });

  it("skips a bar the archive has no volume for", () => {
    const rows = [bar(0, { volume: null })];
    expect(toVolume(toCandles(rows), rows)).toEqual([]);
  });
});

describe("toMarkers", () => {
  const candles = [candle(T0), candle(T0 + 86_400), candle(T0 + 172_800)];

  it("snaps an action to the first candle at or after its date", () => {
    // An action's date is a SESSION date and a candle's time is an
    // instant, so the two never match exactly.
    const [marker] = toMarkers(
      [{ action_date: "2026-09-09", action_type: "DIVIDEND", action_value: "0.25" }],
      candles,
    );
    expect(marker?.time).toBe(T0 + 86_400);
    expect(marker?.kind).toBe(MarkerKind.Dividend);
    expect(marker?.text).toBe("D 0.25");
  });

  it("drops an action after the last candle", () => {
    // Piling it onto the right edge would claim it happened at a time
    // it did not.
    expect(
      toMarkers([{ action_date: "2030-01-01", action_type: "SPLIT", action_value: "4:1" }], candles),
    ).toEqual([]);
  });

  it("ignores an action type it has no shape for", () => {
    expect(
      toMarkers([{ action_date: "2026-09-08", action_type: "MYSTERY", action_value: "1" }], candles),
    ).toEqual([]);
  });

  it("has nothing to snap to without candles", () => {
    expect(
      toMarkers([{ action_date: "2026-09-08", action_type: "DIVIDEND", action_value: "1" }], []),
    ).toEqual([]);
  });
});

describe("gapBands", () => {
  const candles = [candle(T0), candle(T0 + FIVE_MIN * 10)];

  it("opens a slot per interval inside the gap", () => {
    // The time scale only has coordinates for instants some series
    // mentions, and a gap by definition has no bars.
    const gap = {
      gap_start_utc: new Date((T0 + FIVE_MIN) * 1000).toISOString(),
      gap_end_utc: new Date((T0 + FIVE_MIN * 4) * 1000).toISOString(),
    };
    const bands = gapBands([gap], FIVE_MIN, candles);
    expect(bands.whitespace.map((slot) => slot.time)).toEqual([
      T0 + FIVE_MIN,
      T0 + FIVE_MIN * 2,
      T0 + FIVE_MIN * 3,
    ]);
    expect(bands.band).toEqual(bands.whitespace);
  });

  it("never shades an instant that has a candle", () => {
    const gap = {
      gap_start_utc: new Date(T0 * 1000).toISOString(),
      gap_end_utc: new Date((T0 + FIVE_MIN * 2) * 1000).toISOString(),
    };
    const bands = gapBands([gap], FIVE_MIN, candles);
    expect(bands.whitespace.map((slot) => slot.time)).toEqual([T0 + FIVE_MIN]);
  });

  it("stays inside the charted window", () => {
    // Shading before the first candle would stretch the axis to a date
    // the reader did not ask for.
    const gap = {
      gap_start_utc: new Date((T0 - 86_400) * 1000).toISOString(),
      gap_end_utc: new Date((T0 + FIVE_MIN * 2) * 1000).toISOString(),
    };
    const bands = gapBands([gap], FIVE_MIN, candles);
    expect(bands.whitespace.every((slot) => slot.time >= T0)).toBe(true);
  });

  it("says so rather than opening tens of thousands of slots", () => {
    const wide = {
      gap_start_utc: new Date(T0 * 1000).toISOString(),
      gap_end_utc: new Date((T0 + 60 * (GAP_SLOT_LIMIT + 500)) * 1000).toISOString(),
    };
    const bands = gapBands([wide], 60, [candle(T0), candle(T0 + 60 * (GAP_SLOT_LIMIT + 500))]);
    expect(bands.truncated).toBe(true);
    expect(bands.whitespace).toHaveLength(GAP_SLOT_LIMIT);
  });

  it("has nothing to draw without candles", () => {
    const gap = { gap_start_utc: "2026-09-08T00:00:00Z", gap_end_utc: "2026-09-08T01:00:00Z" };
    expect(gapBands([gap], FIVE_MIN, [])).toEqual({
      whitespace: [],
      band: [],
      truncated: false,
    });
  });
});

describe("bucketOf", () => {
  it("floors to the interval", () => {
    expect(bucketOf((T0 + 299) * 1000, FIVE_MIN)).toBe(T0);
    expect(bucketOf((T0 + 300) * 1000, FIVE_MIN)).toBe(T0 + 300);
  });
});

describe("applyTick, intraday", () => {
  const last = candle(T0, { open: 100, high: 110, low: 90, close: 105 });

  it("extends the bar it lands in", () => {
    const applied = applyTick(last, tick({ t: (T0 + 60) * 1000, p: "112" }), FIVE_MIN);
    expect(applied?.isNew).toBe(false);
    expect(applied?.candle).toEqual({ time: T0, open: 100, high: 112, low: 90, close: 112 });
  });

  it("keeps a high the price has come back from", () => {
    // Folding each tick into a running candle, rather than recomputing
    // from the newest one, is what makes the high honest.
    const first = applyTick(last, tick({ t: (T0 + 60) * 1000, p: "120" }), FIVE_MIN);
    const second = applyTick(first?.candle, tick({ t: (T0 + 120) * 1000, p: "101" }), FIVE_MIN);
    expect(second?.candle.high).toBe(120);
    expect(second?.candle.close).toBe(101);
  });

  it("opens a new bucket at the tick's own price", () => {
    const applied = applyTick(last, tick({ t: (T0 + 301) * 1000, p: "107" }), FIVE_MIN);
    expect(applied?.isNew).toBe(true);
    expect(applied?.candle).toEqual({
      time: T0 + 300,
      open: 107,
      high: 107,
      low: 107,
      close: 107,
    });
  });

  it("ignores a tick older than the bar it would extend", () => {
    expect(applyTick(last, tick({ t: (T0 - 600) * 1000 }), FIVE_MIN)).toBeNull();
  });

  it("ignores an extended-hours print", () => {
    // The series was asked for with `session=regular`; a pre-market
    // print would not match the bars under it.
    for (const mh of [MarketHours.PreMarket, MarketHours.PostMarket]) {
      expect(applyTick(last, tick({ mh }), FIVE_MIN)).toBeNull();
    }
  });

  it("ignores a tick with no usable price", () => {
    expect(applyTick(last, tick({ p: "" }), FIVE_MIN)).toBeNull();
  });
});

describe("applyTick, daily", () => {
  // A daily bar's instant is the SESSION open, not UTC midnight.
  const today = candle(T0);

  it("extends today's bar", () => {
    const applied = applyTick(
      today,
      tick({ t: (T0 + 3600) * 1000, p: "112" }),
      86_400,
      BucketMode.Session,
    );
    expect(applied?.isNew).toBe(false);
    expect(applied?.candle.high).toBe(112);
  });

  it("does not invent tomorrow's bar", () => {
    // Which instant it belongs at is the exchange's calendar, which the
    // page does not have; flooring by 86,400 would draw a second candle
    // at UTC midnight beside the real one.
    expect(
      applyTick(today, tick({ t: (T0 + 86_400) * 1000 }), 86_400, BucketMode.Session),
    ).toBeNull();
  });

  it("has nothing to extend on an empty chart", () => {
    expect(applyTick(undefined, tick(), 86_400, BucketMode.Session)).toBeNull();
  });
});
