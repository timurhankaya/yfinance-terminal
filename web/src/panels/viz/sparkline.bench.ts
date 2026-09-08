// @vitest-environment jsdom
//
// The other half of the spec's measurement (`Ölçümler`): what does a
// watchlist-sized page of sparklines cost to draw?
//
// `WLA` holds up to 200 rows and each one carries a month of closes. The
// store side of that page was measured before it was built
// (`live/store.bench.ts`, `docs/measurements/websocket.md`); this is the
// drawing side.
//
// Run it with:  npm --prefix web exec -- vitest bench --run src/panels/viz/sparkline.bench.ts
// Results:      docs/measurements/web-viz.md
//
// The re-render half is NOT here, and deliberately: "a tick does not
// redraw a sparkline" is a property rather than a timing, so it is
// asserted (`viz/Sparkline.test.tsx`, `panels/spark.test.tsx`) the way
// the watchlist's own render isolation is.
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
