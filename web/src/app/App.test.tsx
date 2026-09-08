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
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("AppRoutes", () => {
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
      ["monafy.com", "https://monafy.com/"],
      ["Timurhan Kaya", "https://github.com/kayacekovic"],
    ]);
    for (const a of within(footer).getAllByRole("link")) expect(a.getAttribute("rel")).toBe("noopener noreferrer");
    expect(footer.textContent).toContain("Not affiliated with, endorsed by or connected to Yahoo.");
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

  it("offers every function in a bar; clicking one runs it on the current symbol", async () => {
    mockFetch((url) => {
      if (url.startsWith("/ui/api/v1/symbols/")) {
        const symbol = url.split("/").pop()!;
        return json(200, symbolBody(symbol, `${symbol} Corp`));
      }
      throw new Error(`unexpected ${url}`);
    });
    mount("/ui/t/AAPL/DES");
    await screen.findByText("AAPL Corp");
    const bar = screen.getByRole("navigation", { name: "functions" });
    expect(bar).toHaveTextContent("DES");
    expect(bar).toHaveTextContent("FAKEFA");
    await userEvent.click(within(bar).getByRole("button", { name: "FAKEFA" }));
    expect(await screen.findByText("fake FA panel")).toBeInTheDocument();
    // HELP keeps the symbol, so the bar stays usable from the help page.
    await userEvent.click(within(bar).getByRole("button", { name: "HELP" }));
    expect(await screen.findByRole("heading", { name: "How to use the terminal" })).toBeInTheDocument();
    expect(within(bar).getByRole("button", { name: "DES" })).toBeEnabled();
  });

  it("opens /ui on the home rather than an empty symbol page", async () => {
    // It used to redirect to whatever `localStorage` said was last
    // visited, which was the terminal's only state outside the URL. With
    // a landing page that redirect has nothing left to do.
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
      if (url.startsWith("/ui/api/v1/symbols?q=NOPE")) return json(200, searchBody([{ symbol: "NOPES", long_name: "Nopes Inc.", short_name: null }]));
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
      expect.stringContaining("/ui/api/v1/symbols?q=NOPE&limit=20"),
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
    await waitFor(() => expect(readStore().order).toEqual(["trading"]));
    expect(readStore().pages.trading?.dock).toBeDefined();
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
    // was stored" is not the claim -- "that name was not" is.
    expect(readStore().pages["../etc"]).toBeUndefined();
    expect(readStore().order).not.toContain("../etc");
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

  /** Builds a page through the UI and hands back what was stored.
   *
   *  A layout fixture cannot be hand-written: only dockview can produce
   *  a document dockview will load, and a hand-made one is refused --
   *  which is a real behaviour, tested elsewhere, and a useless fixture
   *  here. */
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

  it("leaves the page alone when the link is not one", async () => {
    stubApi();
    const user = userEvent.setup();
    mount("/ui/w/-?l=not-a-page");
    expect(await screen.findByText(/does not carry a page/)).toBeInTheDocument();
    expect(user).toBeDefined();
  });
});
