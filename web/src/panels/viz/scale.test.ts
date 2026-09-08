import { describe, expect, it } from "vitest";
import { extent, linearScale, niceTicks, normalize100 } from "./scale";

describe("extent", () => {
  it("is null for an empty series, so a caller cannot scale nothing", () => {
    expect(extent([])).toBeNull();
  });

  it("ignores what is not a finite number", () => {
    expect(extent([1, Number.NaN, 5, Number.POSITIVE_INFINITY])).toEqual({ min: 1, max: 5 });
  });

  it("is null when nothing in the series is finite", () => {
    expect(extent([Number.NaN])).toBeNull();
  });
});

describe("linearScale", () => {
  const scale = linearScale({ min: 0, max: 10 }, { min: 0, max: 100 });

  it("maps the ends of the domain onto the ends of the range", () => {
    expect(scale(0)).toBe(0);
    expect(scale(10)).toBe(100);
  });

  it("maps the middle to the middle", () => {
    expect(scale(5)).toBe(50);
  });

  it("inverts when the range does", () => {
    // What every chart here needs: y grows downwards in SVG, so the
    // largest value has to land on the smallest coordinate.
    const y = linearScale({ min: 0, max: 10 }, { min: 20, max: 0 });
    expect(y(0)).toBe(20);
    expect(y(10)).toBe(0);
  });

  it("puts a flat series in the middle of the range rather than at NaN", () => {
    // A day of identical closes is a real series, not an error; dividing
    // by a zero-width domain would draw nothing at all.
    const flat = linearScale({ min: 7, max: 7 }, { min: 0, max: 20 });
    expect(flat(7)).toBe(10);
  });

  it("extrapolates outside the domain rather than clamping", () => {
    // Clamping is the caller's decision: `Bullet` needs to know that a
    // value fell outside its range so it can say so.
    expect(scale(-1)).toBe(-10);
    expect(scale(11)).toBe(110);
  });
});

describe("niceTicks", () => {
  it("lands on round numbers inside the span", () => {
    const ticks = niceTicks(0, 100, 5);
    expect(ticks).toEqual([0, 20, 40, 60, 80, 100]);
  });

  it("never steps by something unreadable", () => {
    const ticks = niceTicks(3, 97, 5);
    const steps = new Set(ticks.slice(1).map((t, i) => t - (ticks[i] ?? 0)));
    expect(steps.size).toBe(1);
    expect([1, 2, 2.5, 5, 10, 20, 25, 50].includes([...steps][0] ?? 0)).toBe(true);
  });

  it("stays inside the span it was given", () => {
    const ticks = niceTicks(3, 97, 5);
    expect(Math.min(...ticks)).toBeGreaterThanOrEqual(3);
    expect(Math.max(...ticks)).toBeLessThanOrEqual(97);
  });

  it("is a single tick for a flat span", () => {
    expect(niceTicks(7, 7, 5)).toEqual([7]);
  });

  it("handles a negative span", () => {
    expect(niceTicks(-10, 10, 5)).toEqual([-10, -5, 0, 5, 10]);
  });
});

describe("normalize100", () => {
  it("starts at 100 and keeps the ratios", () => {
    expect(normalize100([50, 75, 100])).toEqual([100, 150, 200]);
  });

  it("refuses an empty series rather than returning one", () => {
    // The panel is expected to have dropped a symbol with no bars before
    // it gets here; a silent [] would draw a series that is not there.
    expect(() => normalize100([])).toThrow(/empty/i);
  });

  it("refuses a series whose first point is zero", () => {
    expect(() => normalize100([0, 5])).toThrow(/zero/i);
  });
});
