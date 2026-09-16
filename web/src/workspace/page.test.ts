import { describe, expect, it } from "vitest";
import type { SerializedDockview } from "dockview-react";
import { Layout } from "../commands/types";
import { registerPanel } from "../commands/registry";
import { Group } from "./groups";
import {
  PageName,
  SHARE_MAX,
  STORE_VERSION,
  decodePage,
  emptyStore,
  encodePage,
  isPage,
  isPageStore,
  seedsFromDock,
} from "./page";
import type { Page } from "./page";

/** A layout with the panels named, and the grid dockview would have
 *  written around them. Only `panels` is read here; `grid` travels
 *  untouched. */
function dock(panels: Record<string, unknown>): SerializedDockview {
  return {
    grid: { root: { type: "leaf", data: {} }, height: 100, width: 100, orientation: "HORIZONTAL" },
    panels,
  } as unknown as SerializedDockview;
}

function page(over: Partial<Page> = {}): Page {
  return {
    name: PageName.Scratch,
    groups: { [Group.A]: "AAPL" },
    dock: dock({ "gip-1": { id: "gip-1", params: { code: "GIP", symbol: null, args: {}, group: "a" } } }),
    ...over,
  };
}

describe("isPage", () => {
  it("accepts a page and rejects what is not one", () => {
    expect(isPage(page())).toBe(true);
    expect(isPage({ name: "x", groups: {} })).toBe(false);
    expect(isPage(null)).toBe(false);
    expect(isPage([])).toBe(false);
  });

  it("does not judge the layout: only dockview can say whether it loads", () => {
    // The second granularity (Karar 9) is applied where `fromJSON`
    // actually throws, not by guessing here.
    expect(isPage({ name: "x", groups: {}, dock: { nonsense: true } })).toBe(true);
  });
});

describe("isPageStore", () => {
  it("accepts what this build wrote", () => {
    expect(isPageStore(emptyStore())).toBe(true);
    expect(isPageStore({ v: STORE_VERSION, order: ["a"], pages: { a: page({ name: "a" }) } })).toBe(true);
  });

  it("rejects another version outright: there is no migration by decision", () => {
    expect(isPageStore({ v: 2, order: [], pages: {} })).toBe(false);
    expect(isPageStore({ order: [], pages: {} })).toBe(false);
  });

  it("rejects a store whose pages are not pages", () => {
    expect(isPageStore({ v: STORE_VERSION, order: [], pages: { a: { name: "a" } } })).toBe(false);
    expect(isPageStore({ v: STORE_VERSION, order: [1], pages: {} })).toBe(false);
  });
});

describe("seedsFromDock", () => {
  it("reads each panel's command out of the layout's own params", () => {
    const seeds = seedsFromDock(
      dock({
        "des-2": { id: "des-2", params: { code: "DES", symbol: "MSFT", args: {}, group: null } },
        "gip-1": { id: "gip-1", params: { code: "GIP", symbol: null, args: { interval: "5m" }, group: "a" } },
      }),
    );
    expect(seeds.map((seed) => seed.id)).toEqual(["des-2", "gip-1"]);
    expect(seeds[1]).toEqual({
      id: "gip-1",
      code: "GIP",
      symbol: null,
      args: { interval: "5m" },
      group: Group.A,
    });
  });

  it("puts a stored page's args back through the panel's own rules", () => {
    // A stored page is an input `parseArgs` never saw: hand-edited,
    // shared from an older build, or written before the panel changed
    // its mind about what it accepts.
    registerPanel({
      code: "GP",
      title: "GP",
      needsSymbol: true,
      layout: Layout.Single,
      parseArgs: () => ({}),
      normalizeArgs: () => ({ years: "2" }),
      component: () => null,
    });
    const seeds = seedsFromDock(
      dock({ "gp-1": { id: "gp-1", params: { code: "GP", symbol: "AAPL", args: { years: "900" } } } }),
    );
    expect(seeds[0]?.args).toEqual({ years: "2" });
  });

  it("drops a letter that is not one, and a panel with no code", () => {
    const seeds = seedsFromDock(
      dock({
        "a-1": { id: "a-1", params: { code: "DES", symbol: "AAPL", group: "z" } },
        "b-2": { id: "b-2", params: { symbol: "AAPL" } },
        "c-3": "not an object",
      }),
    );
    expect(seeds).toHaveLength(1);
    expect(seeds[0]?.group).toBeNull();
  });

  it("keeps only string arguments: a URL and a store both carry text", () => {
    const seeds = seedsFromDock(
      dock({ "a-1": { id: "a-1", params: { code: "DES", args: { tab: "x", n: 3 } } } }),
    );
    expect(seeds[0]?.args).toEqual({ tab: "x" });
  });

  it("is empty for a layout with no panels at all", () => {
    expect(seedsFromDock(dock({}))).toEqual([]);
  });
});

describe("encodePage / decodePage", () => {
  it("round-trips a page unchanged", () => {
    const original = page({ name: "trading" });
    const encoded = encodePage(original);
    expect(encoded).not.toBeNull();
    expect(decodePage(encoded ?? "")).toEqual(original);
  });

  it("is url-safe, so the link needs no escaping", () => {
    expect(encodePage(page())).toMatch(/^[A-Za-z0-9_-]+$/);
  });

  it("survives a name that is not ASCII", () => {
    const named = page({ name: "günlük" });
    expect(decodePage(encodePage(named) ?? "")?.name).toBe("günlük");
  });

  it("refuses a layout too big to be an address", () => {
    // Half a layout would open as a page that is silently not the one
    // that was shared, so there is no truncated link.
    const huge = page({
      dock: {
        ...page().dock,
        panels: Object.fromEntries(
          Array.from({ length: 400 }, (_, i) => [
            `p${i}`,
            { id: `p${i}`, params: { code: "DES", symbol: `SYMBOL${i}`, args: { padding: "x".repeat(40) } } },
          ]),
        ),
      } as unknown as Page["dock"],
    });
    expect(encodePage(huge)).toBeNull();
    expect((encodePage(page()) ?? "").length).toBeLessThan(SHARE_MAX);
  });

  it("answers null for anything that is not a page", () => {
    expect(decodePage("not base64!")).toBeNull();
    expect(decodePage(btoa("{}"))).toBeNull();
    expect(decodePage("")).toBeNull();
  });
});
