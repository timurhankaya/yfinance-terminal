import { afterEach, describe, expect, it } from "vitest";
import {
  FALLBACK,
  Group,
  GROUP_ORDER,
  divergingHeat,
  groupColor,
  resetVizTheme,
  seriesColors,
  vizTheme,
} from "./colors";

afterEach(() => {
  resetVizTheme();
  document.documentElement.removeAttribute("style");
});

describe("vizTheme", () => {
  it("falls back to the values styles.css sets when nothing is computed", () => {
    // jsdom loads no stylesheet, which is also what a test environment
    // looks like for `Chart.tsx` -- the fallbacks are the same values.
    expect(vizTheme().up).toBe(FALLBACK.up);
    expect(vizTheme().down).toBe(FALLBACK.down);
  });

  it("reads the stylesheet when there is one", () => {
    document.documentElement.style.setProperty("--up", "#00ff00");
    resetVizTheme();
    expect(vizTheme().up).toBe("#00ff00");
  });

  it("reads once and keeps the answer", () => {
    const first = vizTheme();
    expect(vizTheme()).toBe(first);
  });
});

describe("the group palette", () => {
  it("has seven colours, one per group letter", () => {
    expect(GROUP_ORDER).toHaveLength(7);
    expect(new Set(GROUP_ORDER.map(groupColor)).size).toBe(7);
  });

  it("gives a comparison one colour per series, in argument order", () => {
    expect(seriesColors(3)).toEqual([
      groupColor(Group.A),
      groupColor(Group.B),
      groupColor(Group.C),
    ]);
  });

  it("refuses an eighth series rather than repeating a colour", () => {
    // Two series drawn in the same colour is a chart that lies; the
    // panel refuses the command instead.
    expect(() => seriesColors(8)).toThrow(/7/);
  });
});

describe("divergingHeat", () => {
  it("is neutral at zero", () => {
    expect(divergingHeat(0, 10)).toBe(vizTheme().line);
  });

  it("reaches the direction colour at the end of the span", () => {
    expect(divergingHeat(10, 10)).toBe(vizTheme().up);
    expect(divergingHeat(-10, 10)).toBe(vizTheme().down);
  });

  it("clamps beyond the span rather than running off the palette", () => {
    expect(divergingHeat(400, 10)).toBe(vizTheme().up);
    expect(divergingHeat(-400, 10)).toBe(vizTheme().down);
  });

  it("is a colour between the two at half the span", () => {
    const half = divergingHeat(5, 10);
    expect(half).toMatch(/^#[0-9a-f]{6}$/);
    expect(half).not.toBe(vizTheme().line);
    expect(half).not.toBe(vizTheme().up);
  });

  it("is neutral when the span is zero, rather than dividing by it", () => {
    expect(divergingHeat(3, 0)).toBe(vizTheme().line);
  });
});
