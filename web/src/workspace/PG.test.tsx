// The page manager: what it lists, and the two edits a list of pages
// needs.
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { PG, PG_PANEL, normalizeName, pagePath } from "./PG";
import { Layout } from "../commands/types";
import { PAGE_KEYS, readStore, resetStorageBlocked, savePage } from "./store";
import type { Page } from "./page";

function page(name: string, panels: Record<string, unknown> = {}): Page {
  return {
    name,
    groups: { a: "AAPL" },
    dock: { panels } as unknown as Page["dock"],
  };
}

function Where() {
  const { pathname } = useLocation();
  return <span data-testid="where">{pathname}</span>;
}

function draw() {
  return render(
    <MemoryRouter initialEntries={["/ui/m/PG"]}>
      <Where />
      <Routes>
        <Route path="/ui/m/:code" element={<PG symbol={null} args={{}} />} />
        <Route path="*" element={<p>elsewhere</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
  resetStorageBlocked();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("normalizeName", () => {
  it("makes a name that can be an address", () => {
    // The name IS the address, so two names a URL cannot tell apart
    // would be one page with a bug.
    expect(normalizeName("  Trading Desk ")).toBe("trading-desk");
    expect(normalizeName("FX")).toBe("fx");
  });

  it("refuses what an address cannot carry, and the working page's own name", () => {
    expect(normalizeName("")).toBeNull();
    expect(normalizeName("-")).toBeNull();
    expect(normalizeName("../etc")).toBeNull();
    expect(normalizeName("a/b")).toBeNull();
    expect(normalizeName("-leading")).toBeNull();
  });

  it("builds the path a page lives at", () => {
    expect(pagePath("trading")).toBe("/ui/w/trading");
  });
});

describe("PG_PANEL", () => {
  it("is a page in its own right: no symbol, and it shows in the function bar", () => {
    expect(PG_PANEL.code).toBe("PG");
    expect(PG_PANEL.needsSymbol).toBe(false);
    expect(PG_PANEL.layout).toBe(Layout.Single);
  });
});

describe("PG", () => {
  it("says how to make a page when there are none", () => {
    draw();
    expect(screen.getByText(/No saved pages yet/)).toBeInTheDocument();
    expect(screen.getByText(/PG SAVE trading/)).toBeInTheDocument();
  });

  it("lists saved pages with the key that opens each", () => {
    savePage(page("trading", { "gip-1": {}, "des-2": {} }));
    savePage(page("options"));
    draw();
    expect(screen.getByText("trading")).toBeInTheDocument();
    expect(screen.getByText(PAGE_KEYS[0] ?? "")).toBeInTheDocument();
    expect(screen.getByText(PAGE_KEYS[1] ?? "")).toBeInTheDocument();
    expect(screen.getByText(/2 panels · A=AAPL/)).toBeInTheDocument();
  });

  it("opens a page by changing the address, not this panel", () => {
    // A page is what the whole window is; running it inside this panel
    // would put a page inside a page.
    savePage(page("trading"));
    draw();
    fireEvent.click(screen.getByText("trading"));
    expect(screen.getByTestId("where")).toHaveTextContent("/ui/w/trading");
  });

  it("deletes a page and forgets its key", () => {
    savePage(page("trading"));
    savePage(page("options"));
    draw();
    fireEvent.click(screen.getAllByRole("button", { name: "delete" })[0] as HTMLElement);
    expect(readStore().order).toEqual(["options"]);
    expect(screen.queryByText("trading")).not.toBeInTheDocument();
  });

  it("renames a page, keeping its place in the key order", () => {
    savePage(page("trading"));
    savePage(page("options"));
    vi.spyOn(window, "prompt").mockReturnValue("Desk One");
    draw();
    fireEvent.click(screen.getAllByRole("button", { name: "rename" })[0] as HTMLElement);
    expect(readStore().order).toEqual(["desk-one", "options"]);
  });

  it("leaves a page alone when the rename is cancelled or unusable", () => {
    savePage(page("trading"));
    vi.spyOn(window, "prompt").mockReturnValue(null);
    draw();
    fireEvent.click(screen.getByRole("button", { name: "rename" }));
    expect(readStore().order).toEqual(["trading"]);
  });

  it("says so when the browser will not store anything", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked");
    });
    draw();
    expect(screen.getByText(/will not let the terminal store anything/)).toBeInTheDocument();
  });
});
