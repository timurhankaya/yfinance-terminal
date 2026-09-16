// @vitest-environment jsdom
// Benchmarks default to the node environment, and `colors.ts` reads the
// stylesheet through `getComputedStyle`. Layout and render are timed
// separately so a slow answer says WHICH half is slow.
// Run it with:  npx vitest bench src/panels/viz/treemap.bench.ts
import { renderToStaticMarkup } from "react-dom/server";
import { bench, describe } from "vitest";
import { Treemap, capCells, squarify } from "./Treemap";
import type { TreemapItem } from "./Treemap";

const WIDTH = 960;
const HEIGHT = 540;

/** A market-shaped distribution: a few large things and a long tail,
 *  which is what makes squarify work rather than a uniform grid. */
function items(count: number): TreemapItem[] {
  return Array.from({ length: count }, (_, index) => ({
    key: `S${index}`,
    label: `SYM${index}`,
    value: 1_000_000 / (index + 1),
    percent: ((index % 21) - 10) / 2,
  }));
}

describe("squarify, the layout alone", () => {
  for (const size of [50, 200, 400]) {
    const cells = items(size);
    bench(`${size} cells`, () => {
      squarify(cells, WIDTH, HEIGHT);
    });
  }
});

describe("capCells, the cut a bigger list takes first", () => {
  const cells = items(5_000);
  bench("5,000 cells down to 400", () => {
    capCells(cells);
  });
});

describe("layout and render together", () => {
  for (const size of [50, 200, 400]) {
    const cells = items(size);
    // Named apart from the layout suite's: vitest keys a benchmark by its
    // name, and two suites sharing one lose their samples.
    bench(`${size} cells, laid out and rendered`, () => {
      renderToStaticMarkup(
        Treemap({
          items: cells,
          label: "sectors",
          span: 5,
          onOpen: () => {},
          format: (cell) => cell.label,
        }),
      );
    });
  }
});
