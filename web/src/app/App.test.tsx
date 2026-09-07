import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";
import { AppRoutes } from "./App";

const PW = "hunter2";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function symbolBody(symbol: string, longName: string) {
  return { data: { symbol, long_name: longName, short_name: null, exchange: null, full_exchange_name: null, currency: null, quote_type: null, timezone: null, is_active: true, info: null } };
}

function mockFetch(handler: (url: string, init?: RequestInit) => Response) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => handler(String(input), init));
}

function unauthorized(): Response {
  return new Response("{}", { status: 401, headers: { "content-type": "application/problem+json" } });
}

function mount(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AppRoutes />
    </MemoryRouter>,
  );
}

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
});
