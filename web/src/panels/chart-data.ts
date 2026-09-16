// Everything the charts compute, with no chart in sight: jsdom has no
// canvas, so the rules live here as ordinary functions with ordinary
// tests. Times are SECONDS since the epoch, UTC (`UTCTimestamp`); tick
// timestamps arrive in milliseconds and are converted at the door.
import type { Row } from "../api/client";
import { MarketHours } from "../live/types";
import type { Tick } from "../live/types";
// The modules, not the barrel: this file has no components in it and
// must not pull five of them into every panel that imports a
// transform.
import { vizTheme } from "./viz/colors";
import { normalize100 } from "./viz/scale";

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

/** Bars as candles, oldest first, one per instant. `close` is the only
 *  column the archive guarantees; `open`, `high`, `low` fall back to it.
 *  Duplicate instants keep the LAST row: the chart library refuses times
 *  that are not strictly increasing, and a page overlap duplicates. */
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
  // The stylesheet's `--up`/`--down`, like every other colour in the
  // terminal: a volume bar is the same rise as the candle above it, and
  // a hex here would be a second definition of one.
  const { up, down } = vizTheme();
  const bars: VolumeBar[] = [];
  for (const candle of candles) {
    const value = volumes.get(candle.time);
    if (value === undefined) continue;
    bars.push({
      time: candle.time,
      value,
      color: candle.close >= candle.open ? up : down,
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

/** Dividends and splits, snapped to the candle that carries them: an
 *  action's date is a SESSION date and a candle's time an instant, so the
 *  marker goes on the first candle at or after the action. An action
 *  after the last candle is dropped rather than piled on the right edge. */
/** `0.270000000000` -> `0.27`, `2.000000000000` -> `2`; anything that is
 *  not a decimal string is shown as it came. */
export function trimDecimal(value: unknown): string {
  const text = String(value ?? "").trim();
  return /^-?\d+\.\d+$/.test(text) ? text.replace(/0+$/, "").replace(/\.$/, "") : text;
}

export function toMarkers(actions: Row[], candles: Candle[]): ActionMarker[] {
  const times = candles.map((candle) => candle.time);
  // An action before the first candle belongs to a session this window
  // does not show; snapping it onto the first bar would put every
  // dividend of the last decade on one candle.
  const first = times[0];
  if (first === undefined) return [];
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

/** Open gaps, as time slots the chart can shade. A gap has NO bars, so
 *  the time scale has no slot there; whitespace points create the slots,
 *  and the band is a second series with a value at each. Slots outside
 *  the charted window are dropped so the axis is not stretched. */
export function gapBands(gaps: Gap[], intervalSeconds: number, candles: Candle[]): GapBands {
  const whitespace: Whitespace[] = [];
  const first = candles[0]?.time;
  const last = candles[candles.length - 1]?.time;
  if (first === undefined || last === undefined || intervalSeconds <= 0) {
    return { whitespace, band: [], truncated: false };
  }
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
  /** Daily and above: only ever extend the bar that is already there. A
   *  daily bar's instant is the SESSION open, not UTC midnight, so
   *  flooring by 86,400 would draw a second candle; and the next bar's
   *  instant is the exchange calendar's, which this page does not have. */
  Session = "session",
}

function sameUtcDay(a: number, b: number): boolean {
  return Math.floor(a / 86_400) === Math.floor(b / 86_400);
}

/** The last candle, brought up to date by one tick. Null when the tick
 *  cannot move the chart: outside the regular session (the bars were
 *  asked for with `session=regular`), no usable price, older than the bar
 *  it would extend, or in `Session` mode from a day with no bar. A new
 *  bucket opens at the tick's price on all four legs. */
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


// --- the comparison ---------------------------------------------------------

export interface LinePoint {
  time: number;
  value: number;
}

export interface ComparisonSeries {
  symbol: string;
  points: LinePoint[];
  /** The close every point is a percentage of, and the session it was
   *  taken from. Each series is normalised to ITS OWN first session, so
   *  a symbol whose archive starts later still begins at 100 -- and the
   *  panel says which date that was, because otherwise two lines
   *  starting together would imply they started on the same day. */
  base: number;
  baseTime: number;
  /** The whole window's move, in percent: the last point minus 100. */
  changePercent: number;
}

/** One symbol's daily closes as a series indexed to 100 at its first.
 *  Null below two closes: one point is not a shape. Duplicate instants
 *  keep the LAST row, as `toCandles` does and for the same reason. */
export function toComparison(symbol: string, rows: Row[]): ComparisonSeries | null {
  const byTime = new Map<number, number>();
  for (const row of rows) {
    const time = seconds(row.ts_utc);
    const close = num(row.close);
    if (time === null || close === null) continue;
    byTime.set(time, close);
  }
  const sorted = [...byTime.entries()].sort((a, b) => a[0] - b[0]);
  const head = sorted[0];
  if (sorted.length < 2 || head === undefined) return null;
  const [baseTime, base] = head;
  // A first close of zero has no ratio to take. `normalize100` throws on
  // it rather than returning Infinity, so the series is refused here
  // instead and the panel lists the symbol as having no usable bars.
  if (base === 0) return null;
  const indexed = normalize100(sorted.map(([, close]) => close));
  const points: LinePoint[] = [];
  sorted.forEach(([time], i) => {
    const value = indexed[i];
    if (value !== undefined) points.push({ time, value });
  });
  const last = points[points.length - 1];
  if (last === undefined) return null;
  return { symbol, points, base, baseTime, changePercent: last.value - 100 };
}
