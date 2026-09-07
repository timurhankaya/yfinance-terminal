import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DES, formatBig } from "./DES";
import { SessionProvider, useSession } from "../app/session";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

const me = { authenticated: true, expires_at: 1, live_enabled: false };

// vitest.config.ts sets `globals: false`, so @testing-library/react's
// automatic afterEach(cleanup) (which looks for a global `afterEach`)
// never registers; unmount explicitly so tests stay isolated.
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function renderDES(symbol: string) {
  return render(
    <SessionProvider>
      <DES symbol={symbol} />
    </SessionProvider>,
  );
}

describe("formatBig", () => {
  it("scales to K/M/B/T with two decimals", () => {
    expect(formatBig(3_500_000_000_000)).toBe("3.50T");
    expect(formatBig(12_345_678)).toBe("12.35M");
    expect(formatBig(999)).toBe("999");
  });
});

describe("DES", () => {
  it("renders identity and the info fields it knows", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/ui/api/me") return json(200, me);
      if (url === "/v1/symbols/AAPL") return json(200, { data: {
        symbol: "AAPL", long_name: "Apple Inc.", short_name: "Apple", exchange: "NMS",
        full_exchange_name: "NasdaqGS", currency: "USD", quote_type: "EQUITY",
        timezone: "America/New_York", is_active: true,
        info: { sector: "Technology", industry: "Consumer Electronics", marketCap: 3500000000000, trailingPE: 33.1, website: "https://apple.com" },
      } });
      throw new Error(`unexpected ${url}`);
    });
    renderDES("AAPL");
    expect(await screen.findByText("Apple Inc.")).toBeInTheDocument();
    expect(screen.getByText("NasdaqGS")).toBeInTheDocument();
    expect(screen.getByText("Technology")).toBeInTheDocument();
    expect(screen.getByText("3.50T")).toBeInTheDocument();
    expect(screen.getByText("33.10")).toBeInTheDocument();
    expect(screen.queryByText("Beta")).not.toBeInTheDocument();
  });

  it("says so when the symbol does not exist", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/ui/api/me") return json(200, me);
      return new Response(JSON.stringify({ type: "not_found", title: "No such symbol" }), {
        status: 404, headers: { "content-type": "application/problem+json" },
      });
    });
    renderDES("NOPE");
    expect(await screen.findByText(/No such symbol/)).toBeInTheDocument();
  });

  it("shows a retry on a server error", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/ui/api/me") return json(200, me);
      return new Response("{}", { status: 500, headers: { "content-type": "application/problem+json" } });
    });
    renderDES("AAPL");
    expect(await screen.findByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  it("waits for a session and loads once it is there", async () => {
    let authed = false;
    let symbolCalls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/ui/api/me") return json(200, { ...me, authenticated: authed });
      if (url === "/v1/symbols/AAPL") {
        symbolCalls += 1;
        if (!authed) return new Response("{}", { status: 401, headers: { "content-type": "application/problem+json" } });
        return json(200, { data: { symbol: "AAPL", long_name: "Apple Inc.", short_name: null, exchange: null, full_exchange_name: null, currency: null, quote_type: null, timezone: null, is_active: true, info: null } });
      }
      throw new Error(`unexpected ${url}`);
    });
    render(
      <SessionProvider>
        <SessionProbe />
        <DES symbol="AAPL" />
      </SessionProvider>,
    );
    // /me says authenticated:false, so DES must not even try.
    await screen.findByText("session:false");
    expect(symbolCalls).toBe(0);
    authed = true;
    await userEvent.click(screen.getByRole("button", { name: "refresh" }));
    expect(await screen.findByText("Apple Inc.")).toBeInTheDocument();
    expect(symbolCalls).toBe(1);
  });
});

// A tiny consumer of useSession so the test can flip the session.
function SessionProbe() {
  const { me, refresh } = useSession();
  return (
    <>
      <span>session:{String(me?.authenticated ?? "null")}</span>
      <button onClick={() => void refresh()}>refresh</button>
    </>
  );
}
