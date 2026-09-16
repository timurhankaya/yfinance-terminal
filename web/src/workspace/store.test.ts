import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PageName, STORE_VERSION, emptyStore } from "./page";
import type { Page } from "./page";
import {
  PAGE_KEYS,
  SAVE_DEBOUNCE_MS,
  STORE_KEY,
  debounce,
  dropPage,
  pageKeyName,
  readPage,
  readStore,
  renamePage,
  savePage,
} from "./store";

function page(name: string): Page {
  return {
    name,
    groups: {},
    dock: { panels: {} } as unknown as Page["dock"],
  };
}

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("readStore", () => {
  it("is empty when nothing was ever saved", () => {
    expect(readStore()).toEqual(emptyStore());
  });

  it("resets on damage rather than guessing at it", () => {
    // Store-level: the envelope will not parse, or is a version this
    // build does not know. Either way there is nothing to salvage and no
    // migration code by decision (Karar 9).
    window.localStorage.setItem(STORE_KEY, "{ not json");
    expect(readStore()).toEqual(emptyStore());
    window.localStorage.setItem(STORE_KEY, JSON.stringify({ v: 99, order: [], pages: {} }));
    expect(readStore()).toEqual(emptyStore());
  });

  it("reads back what it wrote", () => {
    savePage(page("trading"));
    expect(readStore().v).toBe(STORE_VERSION);
    expect(readPage("trading")?.name).toBe("trading");
    expect(readPage("absent")).toBeNull();
  });
});

describe("savePage", () => {
  it("appends a new page to the key order and keeps its place on re-save", () => {
    // A reader learns "F2 is my options page"; a list that resorted
    // itself on every save would make that untrue.
    savePage(page("first"));
    savePage(page("second"));
    savePage({ ...page("first"), groups: { a: "AAPL" } });
    expect(readStore().order).toEqual(["first", "second"]);
    expect(readPage("first")?.groups).toEqual({ a: "AAPL" });
  });
});

describe("dropPage", () => {
  it("forgets the page and its key", () => {
    savePage(page("a"));
    savePage(page("b"));
    const store = dropPage("a");
    expect(store.order).toEqual(["b"]);
    expect(store.pages.a).toBeUndefined();
  });

  it("does nothing for a page that is not there", () => {
    savePage(page("a"));
    expect(dropPage("nope").order).toEqual(["a"]);
  });
});

describe("renamePage", () => {
  it("keeps the page's place in the key order", () => {
    savePage(page("a"));
    savePage(page("b"));
    const store = renamePage("a", "alpha");
    expect(store.order).toEqual(["alpha", "b"]);
    expect(store.pages.alpha?.name).toBe("alpha");
    expect(store.pages.a).toBeUndefined();
  });

  it("leaves a rename of something absent alone", () => {
    savePage(page("a"));
    expect(renamePage("nope", "x").order).toEqual(["a"]);
  });
});

describe("pageKeyName", () => {
  it("maps the eight keys onto the order", () => {
    // F5, F11 and F12 cannot be cancelled by a page and F6 goes to the
    // address bar, so the eight are F1-F4 and F7-F10.
    expect(PAGE_KEYS).toEqual(["F1", "F2", "F3", "F4", "F7", "F8", "F9", "F10"]);
    const store = { ...emptyStore(), order: ["a", "b"] };
    expect(pageKeyName(store, "F1")).toBe("a");
    expect(pageKeyName(store, "F2")).toBe("b");
    expect(pageKeyName(store, "F3")).toBeNull();
    expect(pageKeyName(store, "F5")).toBeNull();
  });
});

describe("storage that refuses", () => {
  it("says so once and lets the terminal carry on", async () => {
    // The refusal is remembered for the life of the module, so this
    // test gets its own instance rather than marking the shared one.
    vi.resetModules();
    const fresh = await import("./store");
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("blocked");
    });
    expect(fresh.storageBlocked()).toBe(false);
    expect(fresh.writeStore(emptyStore())).toBe(false);
    expect(fresh.storageBlocked()).toBe(true);
  });
});

describe("debounce", () => {
  it("runs once after the calls stop", () => {
    vi.useFakeTimers();
    const fn = vi.fn();
    const wrapped = debounce(SAVE_DEBOUNCE_MS, fn);
    wrapped(1);
    wrapped(2);
    wrapped(3);
    expect(fn).not.toHaveBeenCalled();
    vi.advanceTimersByTime(SAVE_DEBOUNCE_MS);
    expect(fn).toHaveBeenCalledTimes(1);
    expect(fn).toHaveBeenCalledWith(3);
  });

  it("can be cancelled before it fires", () => {
    vi.useFakeTimers();
    const fn = vi.fn();
    const wrapped = debounce(SAVE_DEBOUNCE_MS, fn);
    wrapped();
    wrapped.cancel();
    vi.advanceTimersByTime(SAVE_DEBOUNCE_MS * 4);
    expect(fn).not.toHaveBeenCalled();
  });
});

describe("the scratch page", () => {
  it("has a name, so it can be stored like any other", () => {
    expect(PageName.Scratch).toBe("-");
    savePage(page(PageName.Scratch));
    expect(readPage(PageName.Scratch)).not.toBeNull();
  });
});
