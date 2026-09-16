// @vitest-environment jsdom
//
// What persistence costs a page: one read on the way in, one write per
// arrangement (a whole document, stringified, on a debounce).
// Run it with:  npm --prefix web exec -- vitest bench --run src/workspace/workspace.bench.ts
import { bench, describe } from "vitest";
import type { SerializedDockview } from "dockview-react";
import { decodePage, encodePage, seedsFromDock } from "./page";
import type { Page } from "./page";
import { STORE_KEY } from "./store";

/** A page of `count` panels, in the shape `api.toJSON()` writes. */
function page(count: number): Page {
  const panels: Record<string, unknown> = {};
  for (let i = 0; i < count; i += 1) {
    panels[`gip-${i}`] = {
      id: `gip-${i}`,
      contentComponent: "panel",
      title: `SYM${i} GIP`,
      params: {
        code: "GIP",
        symbol: `SYM${i}`,
        args: { interval: "5m" },
        group: i % 2 === 0 ? "a" : null,
      },
    };
  }
  return {
    name: "trading",
    groups: { a: "AAPL" },
    dock: {
      grid: {
        root: { type: "branch", data: [] },
        height: 900,
        width: 1600,
        orientation: "HORIZONTAL",
      },
      panels,
      activeGroup: "1",
    } as unknown as SerializedDockview,
  };
}

describe("reading a page back", () => {
  for (const size of [1, 4, 8]) {
    const stored = JSON.stringify({ v: 1, order: ["trading"], pages: { trading: page(size) } });
    bench(`${size} panels`, () => {
      const parsed = JSON.parse(stored) as { pages: Record<string, Page> };
      const one = parsed.pages.trading;
      if (one !== undefined) seedsFromDock(one.dock);
    });
  }
});

describe("writing a page out, which is what every drag costs", () => {
  for (const size of [1, 4, 8]) {
    const one = page(size);
    bench(`${size} panels`, () => {
      window.localStorage.setItem(
        STORE_KEY,
        JSON.stringify({ v: 1, order: ["trading"], pages: { trading: one } }),
      );
    });
  }
});

describe("SHARE, which is the same page as an address", () => {
  for (const size of [1, 4, 8]) {
    const one = page(size);
    bench(`${size} panels`, () => {
      const encoded = encodePage(one);
      if (encoded !== null) decodePage(encoded);
    });
  }
});
