// Everything the charts compute, with no chart in sight.
//
// `lightweight-charts` draws to a canvas, which jsdom does not have, so a
// panel test can only assert that a component rendered. The rules worth
// getting right -- which bars become candles, where a corporate action's
// marker lands, how a gap in the archive becomes a shaded band, which
// bucket a live tick belongs in -- live here instead, where they are
// ordinary functions with ordinary tests.
//
// Times are SECONDS since the epoch, UTC, because that is what
// `lightweight-charts` calls a `UTCTimestamp`. Tick timestamps arrive in
// milliseconds and are converted at the door.
import type { Row } from "../api/client";
import { MarketHours } from "../live/types";
import type { Tick } from "../live/types";

export interface Candle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
}

export interface VolumeBar {
  time: number;
  value: number;
  /** Green or red, matching the candle it sits under. */
  color: string;
}

/** A time slot with no data. The time scale only has room for instants
 *  some series mentions, so this is what opens space where the archive
 *  has nothing -- which is exactly where a gap band has to be drawn. */
export interface Whitespace {
  time: number;
}

export enum MarkerKind {
  Dividend = "dividend",
  Split = "split",
  CapitalGain = "capital_gain",
}

export interface ActionMarker {
  time: number;
  kind: MarkerKind;
  text: string;
}

export const UP_COLOR = "#4cc38a";
export const DOWN_COLOR = "#ff6b6b";

function seconds(value: unknown): number | null {
  if (typeof value !== "string") return null;
  const parsed = Date.parse(value.endsWith("Z") || value.includes("+") ? value : `${value}Z`);
  return Number.isNaN(parsed) ? null : Math.floor(parsed / 1000);
}

function num(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value !== "string" || value.trim() === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** Bars as candles, oldest first, one per instant.
 *
 *  `close` is the only column the archive guarantees; `open`, `high` and
 *  `low` are nullable and fall back to it, which draws a flat mark rather
 *  than dropping a bar that has a price in it. Duplicate instants keep
 *  the LAST row: the chart library refuses a series whose times are not
 *  strictly increasing, and a duplicate is what a page overlap looks
 *  like when two cursors meet. */
export function toCandles(rows: Row[]): Candle[] {
  const byTime = new Map<number, Candle>();
  for (const row of rows) {
    const time = seconds(row.ts_utc);
    const close = num(row.close);
    if (time === null || close === null) continue;
    const open = num(row.open) ?? close;
    byTime.set(time, {
      time,
      open,
      close,
      high: num(row.high) ?? Math.max(open, close),
      low: num(row.low) ?? Math.min(open, close),
    });
  }
  return [...byTime.values()].sort((a, b) => a.time - b.time);
}

/** Volume for the same bars, coloured by the candle above it. */
export function toVolume(candles: Candle[], rows: Row[]): VolumeBar[] {
  const volumes = new Map<number, number>();
  for (const row of rows) {
    const time = seconds(row.ts_utc);
    const value = num(row.volume);
    if (time === null || value === null) continue;
    volumes.set(time, value);
  }
  const bars: VolumeBar[] = [];
  for (const candle of candles) {
    const value = volumes.get(candle.time);
    if (value === undefined) continue;
    bars.push({
      time: candle.time,
      value,
      color: candle.close >= candle.open ? UP_COLOR : DOWN_COLOR,
    });
  }
  return bars;
}

const ACTION_KINDS: Record<string, MarkerKind> = {
  DIVIDEND: MarkerKind.Dividend,
  SPLIT: MarkerKind.Split,
  CAPITAL_GAIN: MarkerKind.CapitalGain,
};

const ACTION_PREFIX: Record<MarkerKind, string> = {
  [MarkerKind.Dividend]: "D",
  [MarkerKind.Split]: "S",
  [MarkerKind.CapitalGain]: "CG",
};

/** Dividends and splits, snapped to the candle that carries them.
 *
 *  An action's date is a SESSION date and a candle's time is an instant,
 *  so the two never match exactly. The marker goes on the first candle
 *  at or after the action -- which is the session the price actually
 *  moved in. An action after the last candle has no bar to sit on and is
 *  dropped rather than piled onto the right edge, where it would claim
 *  to have happened at a time it did not. */
/** `0.270000000000` -> `0.27`, `2.000000000000` -> `2`; anything that is
 *  not a decimal string is shown as it came. */
export function trimDecimal(value: unknown): string {
  const text = String(value ?? "").trim();
  return /^-?\d+\.\d+$/.test(text) ? text.replace(/0+$/, "").replace(/\.$/, "") : text;
}

export function toMarkers(actions: Row[], candles: Candle[]): ActionMarker[] {
  if (candles.length === 0) return [];
  const times = candles.map((candle) => candle.time);
  // An action before the first candle belongs to a session this window
  // does not show; snapping it onto the first bar would put every
  // dividend of the last decade on one candle.
  const first = times[0] ?? 0;
  const markers: ActionMarker[] = [];
  for (const action of actions) {
    const raw = action.action_date;
    if (typeof raw !== "string") continue;
    const at = Date.parse(`${raw.slice(0, 10)}T00:00:00Z`) / 1000;
    if (Number.isNaN(at) || at < first) continue;
    const kind = ACTION_KINDS[String(action.action_type).toUpperCase()];
    if (kind === undefined) continue;
    const index = times.findIndex((time) => time >= at);
    if (index === -1) continue;
    const time = times[index];
    if (time === undefined) continue;
    markers.push({
      time,
      kind,
      text: `${ACTION_PREFIX[kind]} ${trimDecimal(action.action_value)}`.trim(),
    });
  }
  return markers;
}

export interface Gap {
  gap_start_utc: string;
  gap_end_utc: string;
}

export interface GapBands {
  /** Empty slots that give the time scale somewhere to draw. */
  whitespace: Whitespace[];
  /** The same instants, as the band's own series. */
  band: Whitespace[];
  /** True when the gaps were too wide to open slot by slot. */
  truncated: boolean;
}

//: A 1m gap of a whole trading day is 390 slots; a month of them is
//: 8,000, and past a few thousand the band costs more than it explains.
export const GAP_SLOT_LIMIT = 3000;

/** Open gaps, as time slots the chart can shade.
 *
 *  A gap is a window where the archive has NO bars, so the time scale
 *  has no slot there and nothing can be drawn: coordinates only exist
 *  for instants some series mentions. Whitespace points are what create
 *  those slots -- the library's own mechanism for it -- and the band is
 *  a second series with a value at each one.
 *
 *  Slots outside the charted window are dropped: shading before the
 *  first candle would stretch the axis to a date the reader did not ask
 *  for. */
export function gapBands(gaps: Gap[], intervalSeconds: number, candles: Candle[]): GapBands {
  const whitespace: Whitespace[] = [];
  if (candles.length === 0 || intervalSeconds <= 0) {
    return { whitespace, band: [], truncated: false };
  }
  const first = candles[0]?.time ?? 0;
  const last = candles[candles.length - 1]?.time ?? 0;
  const known = new Set(candles.map((candle) => candle.time));
  let truncated = false;
  for (const gap of gaps) {
    const from = seconds(gap.gap_start_utc);
    const to = seconds(gap.gap_end_utc);
    if (from === null || to === null || to <= from) continue;
    const start = Math.ceil(Math.max(from, first) / intervalSeconds) * intervalSeconds;
    const end = Math.min(to, last);
    for (let at = start; at < end; at += intervalSeconds) {
      if (known.has(at)) continue;
      if (whitespace.length >= GAP_SLOT_LIMIT) {
        truncated = true;
        break;
      }
      whitespace.push({ time: at });
    }
    if (truncated) break;
  }
  whitespace.sort((a, b) => a.time - b.time);
  return { whitespace, band: whitespace, truncated };
}

/** The bucket a tick belongs in, as a candle time. */
export function bucketOf(epochMs: number, intervalSeconds: number): number {
  const at = Math.floor(epochMs / 1000);
  return Math.floor(at / intervalSeconds) * intervalSeconds;
}

export interface LiveCandle {
  candle: Candle;
  /** True when this opened a new bucket rather than extending the last. */
  isNew: boolean;
}

/** How a tick is placed on the time axis. */
export enum BucketMode {
  /** Intraday: floor the tick to its interval. The chart may open a new
   *  bucket the archive has not written yet -- that is what makes the
   *  last candle move while the market is open. */
  Interval = "interval",
  /** Daily and above: only ever extend the bar that is already there.
   *
   *  A daily bar's instant is the SESSION open (13:30Z for a US listing),
   *  not UTC midnight, so flooring by 86,400 would place today's ticks in
   *  a bucket no bar occupies and draw a second candle beside the real
   *  one. And the chart cannot invent tomorrow's bar: which instant it
   *  belongs at is the exchange's calendar, which this page does not
   *  have. So a tick from a session the archive has no bar for is left
   *  alone until the pipeline writes it. */
  Session = "session",
}

function sameUtcDay(a: number, b: number): boolean {
  return Math.floor(a / 86_400) === Math.floor(b / 86_400);
}

/** The last candle, brought up to date by one tick.
 *
 *  Returns null when the tick cannot move the chart: outside the regular
 *  session (the series was asked for with `session=regular`, so an
 *  extended-hours print would not match the bars under it), without a
 *  usable price, older than the bar it would extend, or -- in
 *  `Session` mode -- from a day the archive has no bar for.
 *
 *  A new bucket opens at the tick's own price on all four legs, which is
 *  what a bar looks like when it has seen one trade. */
export function applyTick(
  last: Candle | undefined,
  tick: Tick,
  intervalSeconds: number,
  mode: BucketMode = BucketMode.Interval,
): LiveCandle | null {
  if (tick.mh !== MarketHours.Regular) return null;
  const price = num(tick.p);
  if (price === null) return null;
  const extend = (base: Candle): LiveCandle => ({
    candle: {
      time: base.time,
      open: base.open,
      high: Math.max(base.high, price),
      low: Math.min(base.low, price),
      close: price,
    },
    isNew: false,
  });

  if (mode === BucketMode.Session) {
    if (last === undefined) return null;
    return sameUtcDay(Math.floor(tick.t / 1000), last.time) ? extend(last) : null;
  }

  const time = bucketOf(tick.t, intervalSeconds);
  if (last === undefined || time > last.time) {
    return { candle: { time, open: price, high: price, low: price, close: price }, isNew: true };
  }
  if (time < last.time) return null;
  return extend(last);
}
