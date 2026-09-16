// The arithmetic every primitive in this directory shares, kept out of
// the components so it is testable without a DOM. No d3-scale, and the
// scales are deliberately not clamped: `Bullet` needs to SEE a value fall
// outside its range so it can say so.

/** A closed interval. Both a domain and a range are one. */
export interface Extent {
  min: number;
  max: number;
}

/** The smallest and largest finite value, or null when there is none.
 *
 *  Null rather than a zero-width extent: "nothing to draw" and "one flat
 *  value" are different pictures, and only the caller knows which of the
 *  two its panel should say. */
export function extent(values: readonly number[]): Extent | null {
  let min = Number.POSITIVE_INFINITY;
  let max = Number.NEGATIVE_INFINITY;
  for (const value of values) {
    if (!Number.isFinite(value)) continue;
    if (value < min) min = value;
    if (value > max) max = value;
  }
  return Number.isFinite(min) ? { min, max } : null;
}

/** A linear map from `domain` onto `range`, unclamped.
 *
 *  A zero-width domain lands in the middle of the range rather than at
 *  NaN: a session of identical closes is a real series, and a flat line
 *  through the middle is what it looks like. */
export function linearScale(domain: Extent, range: Extent): (value: number) => number {
  const span = domain.max - domain.min;
  const middle = (range.min + range.max) / 2;
  if (span === 0) return () => middle;
  const factor = (range.max - range.min) / span;
  return (value) => range.min + (value - domain.min) * factor;
}

//: The steps a reader can add up in their head. Anything else (a 3.7)
//: makes an axis arithmetic rather than a glance.
const STEPS = [1, 2, 2.5, 5, 10];

/** Round tick values inside `[min, max]`, roughly `count` of them.
 *
 *  Inside, not spanning: these label an axis whose ends are the data's
 *  own, so a tick beyond the last bar would point at empty canvas. */
export function niceTicks(min: number, max: number, count: number): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max) || max < min) return [];
  if (max === min) return [min];
  const rough = (max - min) / Math.max(1, count);
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const step = (STEPS.find((s) => s * magnitude >= rough) ?? 10) * magnitude;
  const ticks: number[] = [];
  // Multiply rather than accumulate: adding 0.1 sixteen times does not
  // give 1.6, and an axis labelled 0.7000000000000001 is a bug on screen.
  for (let i = Math.ceil(min / step); i * step <= max; i += 1) {
    ticks.push(Number((i * step).toFixed(10)));
  }
  return ticks;
}

/** Every point as a percentage of the first, which becomes 100.
 *
 *  Throws rather than returning something empty or infinite: a series
 *  with no bars, or one starting at zero, has no comparison to draw, and
 *  the panel is the place that knows what to say about it. */
export function normalize100(values: readonly number[]): number[] {
  const first = values[0];
  if (first === undefined) throw new Error("normalize100: the series is empty");
  if (first === 0) throw new Error("normalize100: the series starts at zero");
  // `value * 100 / first`, not `(value / first) * 100`: the ratio is
  // where the precision goes. 55/50 is 1.1000000000000001 in binary and
  // times 100 that is 110.00000000000001, whereas 5500/50 is 110.
  return values.map((value) => (value * 100) / first);
}
