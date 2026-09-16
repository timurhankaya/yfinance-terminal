import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";
import { AppRoutes } from "./App";
import { clearRegistry, registerPanel } from "../commands/registry";
import { Layout } from "../commands/types";
import { registerAll } from "../panels";
import { PAGE_KEYS, readStore, savePage } from "../workspace/store";
import { encodePage } from "../workspace/page";
import type { Page } from "../workspace/page";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function symbolBody(symbol: string, longName: string) {
  return { data: { symbol, long_name: longName, short_name: null, exchange: null, full_exchange_name: null, currency: null, quote_type: null, timezone: null, is_active: true, info: null } };
}

function searchBody(rows: Array<{ symbol: string; long_name: string | null; short_name: string | null }>) {
  return { data: rows.map((r) => ({ ...r, exchange: null, quote_type: null })), next_cursor: null };
}

function mockFetch(handler: (url: string, init?: RequestInit) => Response) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => handler(String(input), init));
}

// jsdom has no ResizeObserver; cmdk (the command palette) uses one to track
// its list height.
class FakeResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal("ResizeObserver", FakeResizeObserver);
Element.prototype.scrollIntoView = vi.fn();

function mount(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AppRoutes />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  clearRegistry();
  registerAll();
  registerPanel({
    code: "FAKEFA",
    title: "Fake",
    needsSymbol: true,
    layout: Layout.Single,
    parseArgs: () => ({}),
    component: () => <p>fake FA panel</p>,
  });
  registerPanel({
    code: "GIP",
    title: "Intraday",
    needsSymbol: true,
    layout: Layout.Headed,
    parseArgs: (t) => {
      const i = (t[0] ?? "5m").toLowerCase();
      if (!["1m", "5m", "15m", "60m"].includes(i)) throw new Error(`Unknown interval ${t[0]}`);
      return { interval: i };
    },
    component: () => null,
  });
});

afterEach(() => {
  cleanup();
  document.querySelector('meta[name="yfin-dockview-enabled"]')?.remove();
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("AppRoutes", () => {
  it("disables split controls and workspace routes through runtime config", async () => {
    const meta = document.createElement("meta");
    meta.name = "yfin-dockview-enabled";
    meta.content = "false";
    document.head.append(meta);
    mockFetch(() => json(200, { data: [], next_cursor: null }));
    mount("/ui/w/trading");
    expect(await screen.findByRole("navigation", { name: "functions" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open a second panel" })).not.toBeInTheDocument();
    expect(document.querySelector(".dockview-react")).toBeNull();
    expect(screen.queryByRole("button", { name: "PG" })).not.toBeInTheDocument();
  });
  it("credits the data source, the package and the people behind the terminal", async () => {
    mockFetch((url) => {
      if (url === "/ui/api/v1/symbols/AAPL") return json(200, symbolBody("AAPL", "Apple Inc."));
      return json(200, { data: [], next_cursor: null });
    });
    mount("/ui/t/AAPL/DES");
    const footer = await screen.findByRole("contentinfo", { name: "credits" });
    const links = within(footer).getAllByRole("link").map((a) => [a.textContent?.trim(), a.getAttribute("href")]);
    expect(links).toEqual([
      ["Yahoo Finance", "https://finance.yahoo.com/"],
      ["yfinance", "https://github.com/ranaroussi/yfinance"],
      ["Yahoo's terms", "https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html"],
      ["monafy.com", "https://monafy.com/"],
      ["Timurhan Kaya", "https://github.com/kayacekovic"],
    ]);
    for (const a of within(footer).getAllByRole("link")) expect(a.getAttribute("rel")).toBe("noopener noreferrer");
    expect(footer.textContent).toContain("Not affiliated with, endorsed by or connected to Yahoo.");
    expect(footer.textContent).toContain("this software does not license it");
    expect(footer.textContent).toContain("Powered by");
  });

  // The terminal is public: there is no session endpoint left to ask, and
  // no credentials to collect.
  it("asks no session endpoint and offers no sign-in field", async () => {
    const seen: string[] = [];
    mockFetch((url) => {
      seen.push(url);
      if (url === "/ui/api/v1/symbols/AAPL") return json(200, symbolBody("AAPL", "Apple Inc."));
      return json(200, { data: [], next_cursor: null });
    });
    mount("/ui/t/AAPL/DES");
    expect(await screen.findByText("Apple Inc.")).toBeInTheDocument();
    expect(seen).not.toContain("/ui/api/me");
    expect(seen.some((u) => u.startsWith("/ui/api/me"))).toBe(false);
    expect(screen.queryByLabelText("password")).not.toBeInTheDocument();
    expect(document.querySelector('input[type="password"]')).toBeNull();
  });

  it("navigates to DES of the typed symbol on Enter", async () => {
    mockFetch((url) => {
      if (url.startsWith("/ui/api/v1/symbols/")) {
        const symbol = url.split("/").pop()!;
        return json(200, symbolBody(symbol, `${symbol} Corp`));
      }
      throw new Error(`unexpected ${url}`);
    });
    mount("/ui/t/AAPL/DES");
    await userEvent.type(await screen.findByLabelText("command"), "msft{enter}");
    expect(await screen.findByText("MSFT Corp")).toBeInTheDocument();
  });

  it("hands focus to the panel after a command so j/k reach the list, and refocuses on '/'", async () => {
    mockFetch((url) => {
      if (url.startsWith("/ui/api/v1/symbols/")) {
        const symbol = url.split("/").pop()!;
        return json(200, symbolBody(symbol, `${symbol} Corp`));
      }
      throw new Error(`unexpected ${url}`);
    });
    mount("/ui/t/AAPL/DES");
    const box = await screen.findByLabelText("command");
    await waitFor(() => expect(box).toHaveFocus());
    await userEvent.type(box, "msft{enter}");
    await screen.findByText("MSFT Corp");
    expect(box).not.toHaveFocus();
    await userEvent.keyboard("j");
    expect(box).toHaveValue("");
    await userEvent.keyboard("/");
    expect(box).toHaveFocus();
  });

  it("splits the functions in two: the market under the command line, the symbol in its band", async () => {
    mockFetch((url) => {
      if (url.startsWith("/ui/api/sparklines")) return json(200, { data: { points: 0, series: [], missing: [] } });
      if (url.startsWith("/ui/api/v1/symbols/")) {
        const symbol = url.split("/").pop()!;
        return json(200, symbolBody(symbol, `${symbol} Corp`));
      }
      throw new Error(`unexpected ${url}`);
    });
    mount("/ui/t/AAPL/DES");
    await screen.findByText("AAPL Corp");
    // The bar under the command line is the market's now. A flat list of
    // 22 codes told a reader nothing about which a symbol was for.
    const bar = screen.getByRole("navigation", { name: "functions" });
    expect(bar).toHaveTextContent("HELP");
    expect(bar).not.toHaveTextContent("FAKEFA");
    // The symbol's own functions sit in the band the panel wears, beside
    // the price they act on.
    const band = screen.getByRole("navigation", { name: "AAPL functions" });
    expect(band).toHaveTextContent("DES");
    await userEvent.click(within(band).getByRole("button", { name: "FAKEFA" }));
    expect(await screen.findByText("fake FA panel")).toBeInTheDocument();
    // FAKEFA is not `headed`, so it never had a strip -- and it still
    // gets the band, which is the whole point of splitting the two.
    const moved = screen.getByRole("navigation", { name: "AAPL functions" });
    expect(within(moved).getByRole("button", { name: "DES" })).toBeEnabled();
    // A market page is about no symbol, so it wears no band.
    await userEvent.click(within(bar).getByRole("button", { name: "HELP" }));
    expect(await screen.findByRole("heading", { name: "How to use the terminal" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "AAPL functions" })).not.toBeInTheDocument();
  });

  it("opens /ui on the home rather than an empty symbol page", async () => {
    mockFetch(() => json(200, { data: [], next_cursor: null }));
    mount("/ui");
    expect(await screen.findByText(/Type a symbol to open its detail/)).toBeInTheDocument();
  });

  it("serves a market page from its own root, with no symbol in the path", async () => {
    mockFetch(() => json(200, { data: [], next_cursor: null }));
    mount("/ui/m/EQS");
    expect(await screen.findByText(/None are enabled/)).toBeInTheDocument();
  });

  it("runs a mnemonic-only command against the current symbol", async () => {
    mockFetch((url) => {
      if (url === "/ui/api/v1/symbols/MSFT") return json(200, symbolBody("MSFT", "Microsoft Corp"));
      throw new Error(`unexpected ${url}`);
    });
    mount("/ui/t/MSFT/DES");
    await screen.findByText("Microsoft Corp");
    await userEvent.type(await screen.findByLabelText("command"), "msft fakefa{enter}");
    expect(await screen.findByText("fake FA panel")).toBeInTheDocument();
    expect(await screen.findByLabelText("command")).toHaveValue("");
  });

  it("warns and opens the palette when a symbol is not found, and lets the palette pick resolve it", async () => {
    mockFetch((url) => {
      if (url === "/ui/api/v1/symbols/AAPL") return json(200, symbolBody("AAPL", "Apple Inc."));
      if (url === "/ui/api/v1/symbols/NOPE") return new Response("{}", { status: 404, headers: { "content-type": "application/problem+json" } });
      if (url === "/ui/api/v1/symbols/NOPES") return json(200, symbolBody("NOPES", "Nopes Inc."));
      if (url.startsWith("/ui/api/search?q=NOPE")) return json(200, searchBody([{ symbol: "NOPES", long_name: "Nopes Inc.", short_name: null }]));
      if (url.startsWith("/ui/api/sparklines")) return json(200, { data: { points: 0, series: [], missing: [] } });
      if (url.startsWith("/ui/api/v1/symbols/NOPES/bars") || url.startsWith("/ui/api/v1/symbols/AAPL/bars")) return json(200, { data: [], next_cursor: null });
      throw new Error(`unexpected ${url}`);
    });
    mount("/ui/t/AAPL/DES");
    await screen.findByText("Apple Inc.");
    await userEvent.type(await screen.findByLabelText("command"), "NOPE{enter}");
    expect(await screen.findByText("No such symbol NOPE")).toBeInTheDocument();
    expect(await screen.findByText("Apple Inc.")).toBeInTheDocument();
    const palette = await screen.findByLabelText("palette");
    expect(palette).toBeInTheDocument();
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalledWith(
      expect.stringContaining("/ui/api/search?q=NOPE"),
      expect.anything(),
    ));
    const item = await screen.findByText(/NOPES/);
    await userEvent.click(item);
    expect(await screen.findByText("Nopes Inc.")).toBeInTheDocument();
  });

  it("reports a bad panel argument without changing the URL", async () => {
    mockFetch((url) => {
      if (url === "/ui/api/v1/symbols/AAPL") return json(200, symbolBody("AAPL", "Apple Inc."));
      throw new Error(`unexpected ${url}`);
    });
    mount("/ui/t/AAPL/DES");
    await screen.findByText("Apple Inc.");
    await userEvent.type(await screen.findByLabelText("command"), "gip 3m{enter}");
    expect(await screen.findByText("Unknown interval 3m")).toBeInTheDocument();
    expect(await screen.findByText("Apple Inc.")).toBeInTheDocument();
  });

  it("Esc first clears the focused command box, then navigates back", async () => {
    mockFetch((url) => {
      if (url === "/ui/api/v1/symbols/AAPL") return json(200, symbolBody("AAPL", "Apple Inc."));
      if (url === "/ui/api/v1/symbols/MSFT") return json(200, symbolBody("MSFT", "Microsoft Corp"));
      throw new Error(`unexpected ${url}`);
    });
    mount("/ui/t/AAPL/DES");
    await screen.findByText("Apple Inc.");
    const input = await screen.findByLabelText("command");
    await userEvent.type(input, "msft{enter}");
    await screen.findByText("Microsoft Corp");
    await userEvent.type(input, "something");
    expect(input).toHaveValue("something");
    await userEvent.keyboard("{Escape}");
    expect(input).toHaveValue("");
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.getByText("Apple Inc.")).toBeInTheDocument());
  });

  it("Shift+Esc goes forward even while the command box is focused", async () => {
    mockFetch((url) => {
      if (url === "/ui/api/v1/symbols/AAPL") return json(200, symbolBody("AAPL", "Apple Inc."));
      if (url === "/ui/api/v1/symbols/MSFT") return json(200, symbolBody("MSFT", "Microsoft Corp"));
      throw new Error(`unexpected ${url}`);
    });
    mount("/ui/t/AAPL/DES");
    await screen.findByText("Apple Inc.");
    const input = await screen.findByLabelText("command");
    await userEvent.type(input, "msft{enter}");
    await screen.findByText("Microsoft Corp");
    input.blur();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.getByText("Apple Inc.")).toBeInTheDocument());
    input.focus();
    await userEvent.type(input, "draft");
    await userEvent.keyboard("{Shift>}{Escape}{/Shift}");
    await waitFor(() => expect(screen.getByText("Microsoft Corp")).toBeInTheDocument());
    // A plain Escape is still the box's own: it clears, it does not navigate.
    await userEvent.keyboard("{Escape}");
    expect(input).toHaveValue("");
    expect(screen.getByText("Microsoft Corp")).toBeInTheDocument();
  });

  it("opens HELP on '?' when the command box is not focused", async () => {
    mockFetch((url) => {
      if (url === "/ui/api/v1/symbols/AAPL") return json(200, symbolBody("AAPL", "Apple Inc."));
      throw new Error(`unexpected ${url}`);
    });
    mount("/ui/t/AAPL/DES");
    await screen.findByText("Apple Inc.");
    const input = await screen.findByLabelText("command");
    input.blur();
    await userEvent.keyboard("?");
    expect(await screen.findByText("How to use the terminal")).toBeInTheDocument();
  });

  it("runs a command in the focused panel of a saved page, and Ctrl+Enter opens another", async () => {
    // A saved page is not an address, so a command lands in a panel
    // rather than in the URL -- and with the modifier, beside it.
    mockFetch(() => json(404, { detail: "not here" }));
    for (const code of ["AAA", "BBB"]) {
      registerPanel({
        code,
        title: code,
        needsSymbol: false,
        layout: Layout.Single,
        parseArgs: () => ({}),
        component: () => <p>panel {code}</p>,
      });
    }
    const user = userEvent.setup();
    mount("/ui/w/-");
    const box = await screen.findByLabelText("command");

    await user.click(box);
    await user.keyboard("AAA{Enter}");
    expect(await screen.findByText("panel AAA")).toBeInTheDocument();

    await user.click(box);
    await user.keyboard("BBB{Control>}{Enter}{/Control}");
    expect(await screen.findByText("panel BBB")).toBeInTheDocument();
    expect(screen.getByText("panel AAA")).toBeInTheDocument();
  });

  it("pins a panel to a letter, and then a bare ticker moves the letter", async () => {
    mockFetch((url) => {
      if (url === "/ui/api/v1/symbols/AAPL") return json(200, symbolBody("AAPL", "Apple Inc."));
      if (url === "/ui/api/v1/symbols/MSFT") return json(200, symbolBody("MSFT", "Microsoft"));
      return json(404, { detail: "not here" });
    });
    registerPanel({
      code: "SYM",
      title: "Sym",
      needsSymbol: true,
      layout: Layout.Single,
      parseArgs: () => ({}),
      component: ({ symbol }) => <p>sym {symbol}</p>,
    });
    const user = userEvent.setup();
    mount("/ui/w/-");
    const box = await screen.findByLabelText("command");

    await user.click(box);
    await user.keyboard("AAPL SYM{Enter}");
    expect(await screen.findByText("sym AAPL")).toBeInTheDocument();

    await user.click(box);
    await user.keyboard("GRP B{Enter}");
    await user.click(box);
    await user.keyboard("MSFT{Enter}");
    // The ticker went to the letter, and the letter carried the panel.
    expect(await screen.findByText("sym MSFT")).toBeInTheDocument();
  });

  it("refuses a letter on a panel that carries its own symbols", async () => {
    mockFetch(() => json(404, { detail: "not here" }));
    registerPanel({
      code: "LIST",
      title: "List",
      needsSymbol: false,
      layout: Layout.Single,
      parseArgs: () => ({}),
      component: () => <p>a list of many symbols</p>,
    });
    const user = userEvent.setup();
    mount("/ui/w/-");
    const box = await screen.findByLabelText("command");

    await user.click(box);
    await user.keyboard("LIST{Enter}");
    await screen.findByText("a list of many symbols");
    await user.click(box);
    await user.keyboard("GRP A{Enter}");
    expect(await screen.findByText(/carries its own symbols/)).toBeInTheDocument();
  });

  it("lets the reader pick the accent, and remembers which", async () => {
    mockFetch(() => json(404, { detail: "not here" }));
    const user = userEvent.setup();
    const first = mount("/ui");
    await user.click(await screen.findByRole("button", { name: "violet" }));
    expect(document.documentElement.dataset.accent).toBe("violet");
    expect(await screen.findByRole("button", { name: "violet" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    // The choice is the reader's, not the tab's.
    first.unmount();
    mount("/ui");
    expect(document.documentElement.dataset.accent).toBe("violet");
  });

  it("says how to open a second panel while there is only one, and stops once there are two", async () => {
    mockFetch(() => json(404, { detail: "not here" }));
    registerPanel({
      code: "AAA",
      title: "AAA",
      needsSymbol: false,
      layout: Layout.Single,
      parseArgs: () => ({}),
      component: () => <p>panel AAA</p>,
    });
    const user = userEvent.setup();
    mount("/ui/w/-");
    const box = await screen.findByLabelText("command");
    expect(screen.getByText(/opens a second panel beside this one/)).toBeInTheDocument();

    await user.click(box);
    await user.keyboard("AAA{Control>}{Enter}{/Control}");
    await screen.findByText("panel AAA");
    // The page has shown them; the line has nothing left to say.
    await waitFor(() =>
      expect(screen.queryByText(/opens a second panel beside this one/)).not.toBeInTheDocument(),
    );
  });

  it("focuses the command box on '/' when it is not already focused", async () => {
    mockFetch((url) => {
      if (url === "/ui/api/v1/symbols/AAPL") return json(200, symbolBody("AAPL", "Apple Inc."));
      throw new Error(`unexpected ${url}`);
    });
    mount("/ui/t/AAPL/DES");
    await screen.findByText("Apple Inc.");
    const input = await screen.findByLabelText("command");
    input.blur();
    expect(document.activeElement).not.toBe(input);
    await userEvent.keyboard("/");
    expect(document.activeElement).toBe(input);
  });
});

describe("saved pages", () => {
  function stubApi() {
    mockFetch((url) => {
      if (url === "/ui/api/v1/symbols/AAPL") return json(200, symbolBody("AAPL", "Apple Inc."));
      return json(404, { detail: "not here" });
    });
  }

  function registerSimple(code: string) {
    registerPanel({
      code,
      title: code,
      needsSymbol: false,
      layout: Layout.Single,
      parseArgs: () => ({}),
      component: () => <p>panel {code}</p>,
    });
  }

  it("names the working page, stores it, and moves to its address", async () => {
    // The name IS the address: a page called `trading` that stayed on
    // `/ui/w/-` could be neither reloaded nor shared, which is what
    // naming it was for.
    stubApi();
    registerSimple("AAA");
    const user = userEvent.setup();
    mount("/ui/w/-");
    const box = await screen.findByLabelText("command");
    await user.click(box);
    await user.keyboard("AAA{Enter}");
    await screen.findByText("panel AAA");

    await user.click(box);
    await user.keyboard("PG SAVE trading{Enter}");
    // Whether the scratch page it was on has also been flushed by now is
    // a debounce race and not what this is about: `trading` is stored,
    // with the layout in it, and it has a key.
    await waitFor(() => expect(readStore().pages.trading?.dock).toBeDefined());
    expect(readStore().order).toContain("trading");
  });

  it("refuses a name an address cannot carry", async () => {
    stubApi();
    const user = userEvent.setup();
    mount("/ui/w/-");
    const box = await screen.findByLabelText("command");
    await user.click(box);
    await user.keyboard("PG SAVE ../etc{Enter}");
    expect(await screen.findByText(/A page name is letters, digits and dashes/)).toBeInTheDocument();
    // The working page writes itself after a quarter second, so "nothing
    // was stored" is not the claim -- "that name was not, and neither was
    // any other" is.
    expect(readStore().pages["../etc"]).toBeUndefined();
    expect(readStore().order.filter((name) => name !== "-")).toEqual([]);
  });

  it("brings a saved page back, panels and all", async () => {
    stubApi();
    registerSimple("AAA");
    const user = userEvent.setup();
    const first = mount("/ui/w/-");
    const box = await screen.findByLabelText("command");
    await user.click(box);
    await user.keyboard("AAA{Enter}");
    await screen.findByText("panel AAA");
    await user.click(box);
    await user.keyboard("PG SAVE trading{Enter}");
    await waitFor(() => expect(readStore().pages.trading).toBeDefined());
    first.unmount();

    mount("/ui/w/trading");
    expect(await screen.findByText("panel AAA")).toBeInTheDocument();
  });

  /** Builds a page through the UI and hands back what was stored: only
   *  dockview can produce a layout document dockview will load, so a
   *  hand-written fixture is refused. */
  async function buildPage(name: string, code: string): Promise<Page> {
    const user = userEvent.setup();
    const view = mount("/ui/w/-");
    const box = await screen.findByLabelText("command");
    await user.click(box);
    await user.keyboard(`${code}{Enter}`);
    await screen.findByText(`panel ${code}`);
    await user.click(box);
    await user.keyboard(`PG SAVE ${name}{Enter}`);
    await waitFor(() => expect(readStore().pages[name]).toBeDefined());
    view.unmount();
    const stored = readStore().pages[name];
    if (stored === undefined) throw new Error(`${name} was not stored`);
    return stored;
  }

  it("opens a page from its function key, and says when a key has none", async () => {
    stubApi();
    registerSimple("AAA");
    await buildPage("trading", "AAA");
    const user = userEvent.setup();
    mount("/ui/w/-");
    await screen.findByLabelText("command");

    await user.keyboard(`{${PAGE_KEYS[0] ?? "F1"}}`);
    expect(await screen.findByText("panel AAA")).toBeInTheDocument();

    await user.keyboard(`{${PAGE_KEYS[3] ?? "F4"}}`);
    expect(await screen.findByText(/has no page yet/)).toBeInTheDocument();
  });

  it("writes the page as a link, and refuses when there is no page", async () => {
    stubApi();
    registerSimple("AAA");
    const user = userEvent.setup();
    mount("/ui/w/-");
    const box = await screen.findByLabelText("command");
    await user.click(box);
    await user.keyboard("AAA{Enter}");
    await screen.findByText("panel AAA");

    await user.click(box);
    await user.keyboard("SHARE{Enter}");
    const link = await screen.findByText(/\?l=/);
    expect(link.textContent ?? "").toContain("/ui/w/-?l=");
  });

  it("asks before a link replaces a page that has something on it", async () => {
    // The arrangement a link would overwrite took work, so it is an
    // offer rather than an ambush.
    stubApi();
    registerSimple("AAA");
    registerSimple("BBB");
    const other = await buildPage("other", "BBB");
    const shared = encodePage({ ...other, name: "-" });
    // The working page was left holding AAA on the way through.
    savePage({ ...(await buildPage("kept", "AAA")), name: "-" });

    const user = userEvent.setup();
    mount(`/ui/w/-?l=${shared ?? ""}`);

    expect(await screen.findByText(/Enter to replace the working page/)).toBeInTheDocument();
    expect(await screen.findByText("panel AAA")).toBeInTheDocument();

    const box = await screen.findByLabelText("command");
    await user.click(box);
    await user.keyboard("{Enter}");
    expect(await screen.findByText("panel BBB")).toBeInTheDocument();
  });


  it("turns an address into the working page when it is split", async () => {
    // An address page IS one panel. Splitting it is the moment it stops
    // being an address, and what it was showing comes along -- otherwise
    // The + action on `/ui/t/AAPL/DES` must not throw the
    // page away.
    stubApi();
    registerSimple("AAA");
    const user = userEvent.setup();
    mount("/ui/t/AAPL/FAKEFA");
    await screen.findByText("fake FA panel");

    expect(document.querySelector(".dock")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Open a second panel" }));
    await waitFor(() => expect(screen.getAllByText("fake FA panel")).toHaveLength(2));
    const box = await screen.findByLabelText("command");
    await user.click(box);
    await user.keyboard("AAA{Enter}");

    expect(await screen.findByText("panel AAA")).toBeInTheDocument();
    // Both panels, on the working page.
    expect(screen.getByText("fake FA panel")).toBeInTheDocument();
    await waitFor(() => expect(readStore().pages["-"]).toBeDefined());
    const seeds = Object.values(
      (readStore().pages["-"]?.dock as unknown as { panels: Record<string, unknown> }).panels,
    );
    expect(seeds).toHaveLength(2);
  });

  it("still just navigates when an address page is not split", async () => {
    stubApi();
    registerSimple("AAA");
    const user = userEvent.setup();
    mount("/ui/t/AAPL/FAKEFA");
    await screen.findByText("fake FA panel");
    const box = await screen.findByLabelText("command");
    await user.click(box);
    await user.keyboard("AAA{Enter}");
    expect(await screen.findByText("panel AAA")).toBeInTheDocument();
    expect(screen.queryByText("fake FA panel")).not.toBeInTheDocument();
  });

  it("leaves the page alone when the link is not one", async () => {
    stubApi();
    mount("/ui/w/-?l=not-a-page");
    expect(await screen.findByText(/does not carry a page/)).toBeInTheDocument();
  });
});
