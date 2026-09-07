import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";
import { AppRoutes } from "./App";
import { clearRegistry, registerPanel } from "../commands/registry";
import { registerAll } from "../panels";

const PW = "hunter2";

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

function unauthorized(): Response {
  return new Response("{}", { status: 401, headers: { "content-type": "application/problem+json" } });
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
    layout: "single",
    parseArgs: () => ({}),
    component: () => <p>fake FA panel</p>,
  });
  registerPanel({
    code: "GIP",
    title: "Intraday",
    needsSymbol: true,
    layout: "headed",
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
  it("shows the login modal when unauthenticated", async () => {
    mockFetch((url) => {
      if (url === "/ui/api/me") return json(200, { authenticated: false, expires_at: null, live_enabled: false });
      throw new Error(`unexpected ${url}`);
    });
    mount("/ui/t/AAPL/DES");
    expect(await screen.findByLabelText("password")).toBeInTheDocument();
  });

  it("logs in and reveals the shell with the symbol", async () => {
    let authed = false;
    mockFetch((url, init) => {
      if (url === "/ui/api/me") return json(200, { authenticated: authed, expires_at: authed ? 1 : null, live_enabled: false });
      if (url === "/ui/api/login" && init?.method === "POST") {
        authed = true;
        return new Response(null, { status: 204 });
      }
      // Like the real API: no session, no data. This is what proves DES
      // loads AFTER login rather than before the modal appeared.
      if (url === "/v1/symbols/AAPL") return authed ? json(200, symbolBody("AAPL", "Apple Inc.")) : unauthorized();
      throw new Error(`unexpected ${url}`);
    });
    mount("/ui/t/AAPL/DES");
    await userEvent.type(await screen.findByLabelText("password"), `${PW}{enter}`);
    await waitFor(() => expect(screen.queryByLabelText("password")).not.toBeInTheDocument());
    expect(await screen.findByText("Apple Inc.")).toBeInTheDocument();
  });

  it("navigates to DES of the typed symbol on Enter", async () => {
    mockFetch((url) => {
      if (url === "/ui/api/me") return json(200, { authenticated: true, expires_at: 1, live_enabled: false });
      if (url.startsWith("/v1/symbols/")) {
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
      if (url === "/ui/api/me") return json(200, { authenticated: true, expires_at: 1, live_enabled: false });
      if (url.startsWith("/v1/symbols/")) {
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
      if (url === "/ui/api/me") return json(200, { authenticated: true, expires_at: 1, live_enabled: false });
      if (url.startsWith("/v1/symbols/")) {
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
    expect(await screen.findByRole("heading", { name: "Help" })).toBeInTheDocument();
    expect(within(bar).getByRole("button", { name: "DES" })).toBeEnabled();
  });

  it("gives the password field focus on first load, not the command box", async () => {
    mockFetch((url) => {
      if (url === "/ui/api/me") return json(200, { authenticated: false, expires_at: null, live_enabled: false });
      throw new Error(`unexpected ${url}`);
    });
    mount("/ui/t/AAPL/DES");
    const password = await screen.findByLabelText("password");
    await waitFor(() => expect(password).toHaveFocus());
    expect(screen.getByLabelText("command")).not.toHaveFocus();
  });

  it("shows an unreachable-API message when login fails with a network error", async () => {
    mockFetch((url) => {
      if (url === "/ui/api/me") return json(200, { authenticated: false, expires_at: null, live_enabled: false });
      if (url === "/ui/api/login") throw new TypeError("network");
      throw new Error(`unexpected ${url}`);
    });
    mount("/ui/t/AAPL/DES");
    await userEvent.type(await screen.findByLabelText("password"), `${PW}{enter}`);
    expect(await screen.findByText("Could not reach the API. Try again.")).toBeInTheDocument();
  });

  it("redirects /ui to the last visited triple", async () => {
    localStorage.setItem("yfin.ui.last", "/ui/t/TSLA/DES");
    mockFetch((url) => {
      if (url === "/ui/api/me") return json(200, { authenticated: true, expires_at: 1, live_enabled: false });
      if (url === "/v1/symbols/TSLA") return json(200, symbolBody("TSLA", "Tesla"));
      throw new Error(`unexpected ${url}`);
    });
    mount("/ui");
    expect(await screen.findByText("Tesla")).toBeInTheDocument();
  });

  it("runs a mnemonic-only command against the current symbol", async () => {
    mockFetch((url) => {
      if (url === "/ui/api/me") return json(200, { authenticated: true, expires_at: 1, live_enabled: false });
      if (url === "/v1/symbols/MSFT") return json(200, symbolBody("MSFT", "Microsoft Corp"));
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
      if (url === "/ui/api/me") return json(200, { authenticated: true, expires_at: 1, live_enabled: false });
      if (url === "/v1/symbols/AAPL") return json(200, symbolBody("AAPL", "Apple Inc."));
      if (url === "/v1/symbols/NOPE") return new Response("{}", { status: 404, headers: { "content-type": "application/problem+json" } });
      if (url === "/v1/symbols/NOPES") return json(200, symbolBody("NOPES", "Nopes Inc."));
      if (url.startsWith("/v1/symbols?q=NOPE")) return json(200, searchBody([{ symbol: "NOPES", long_name: "Nopes Inc.", short_name: null }]));
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
      expect.stringContaining("/v1/symbols?q=NOPE&limit=20"),
      expect.anything(),
    ));
    const item = await screen.findByText(/NOPES/);
    await userEvent.click(item);
    expect(await screen.findByText("Nopes Inc.")).toBeInTheDocument();
  });

  it("reports a bad panel argument without changing the URL", async () => {
    mockFetch((url) => {
      if (url === "/ui/api/me") return json(200, { authenticated: true, expires_at: 1, live_enabled: false });
      if (url === "/v1/symbols/AAPL") return json(200, symbolBody("AAPL", "Apple Inc."));
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
      if (url === "/ui/api/me") return json(200, { authenticated: true, expires_at: 1, live_enabled: false });
      if (url === "/v1/symbols/AAPL") return json(200, symbolBody("AAPL", "Apple Inc."));
      if (url === "/v1/symbols/MSFT") return json(200, symbolBody("MSFT", "Microsoft Corp"));
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

  it("opens HELP on '?' when the command box is not focused", async () => {
    mockFetch((url) => {
      if (url === "/ui/api/me") return json(200, { authenticated: true, expires_at: 1, live_enabled: false });
      if (url === "/v1/symbols/AAPL") return json(200, symbolBody("AAPL", "Apple Inc."));
      throw new Error(`unexpected ${url}`);
    });
    mount("/ui/t/AAPL/DES");
    await screen.findByText("Apple Inc.");
    const input = await screen.findByLabelText("command");
    input.blur();
    await userEvent.keyboard("?");
    expect(await screen.findByText("Help")).toBeInTheDocument();
  });

  it("focuses the command box on '/' when it is not already focused", async () => {
    mockFetch((url) => {
      if (url === "/ui/api/me") return json(200, { authenticated: true, expires_at: 1, live_enabled: false });
      if (url === "/v1/symbols/AAPL") return json(200, symbolBody("AAPL", "Apple Inc."));
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
