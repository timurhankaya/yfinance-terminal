// @vitest-environment jsdom
//
// What a watchlist-sized page of sparklines costs to draw: up to 200
// rows, each carrying a month of closes.
// Run it with:  npm --prefix web exec -- vitest bench --run src/panels/viz/sparkline.bench.ts
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { bench, describe } from "vitest";
import { Sparkline } from "./Sparkline";

/** A month of closes with a shape, not a straight line: the path builder
 *  writes one point per session either way, but a real series is what a
 *  cell holds. */
function closes(seed: number, points: number): number[] {
  return Array.from({ length: points }, (_, index) => 100 + Math.sin((index + seed) / 3) * 5);
}

describe("a page of sparklines, mounted", () => {
  for (const rows of [20, 100, 200]) {
    const series = Array.from({ length: rows }, (_, index) => closes(index, 30));
    bench(`${rows} rows x 30 points`, () => {
      renderToStaticMarkup(
        createElement(
          "div",
          null,
          series.map((values, index) =>
            createElement(Sparkline, { key: index, values, label: `SYM${index}` }),
          ),
        ),
      );
    });
  }
});

describe("one cell, by window length", () => {
  for (const points of [5, 30, 90]) {
    const values = closes(0, points);
    bench(`${points} points`, () => {
      renderToStaticMarkup(createElement(Sparkline, { values, label: "SYM" }));
    });
  }
});
