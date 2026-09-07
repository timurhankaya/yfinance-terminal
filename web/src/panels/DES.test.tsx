import { cleanup, render, screen } from "@testing-library/react";
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
      <DES symbol={symbol} args={{}} />
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
      if (url === "/ui/api/v1/symbols/AAPL") return json(200, { data: {
        symbol: "AAPL", long_name: "Apple Inc.", short_name: "Apple", exchange: "NMS",
        full_exchange_name: "NasdaqGS", currency: "USD", quote_type: "EQUITY",
        timezone: "America/New_York", is_active: true,
        // Shaped like the real API: snake_case keys, Decimal numbers as
        // strings, dividend_yield already a percentage.
        info: {
          sector: "Technology", industry: "Consumer Electronics",
          market_cap: "4669700046848", trailing_pe: "36.609840000000",
          dividend_yield: "0.340000000000", website: "https://apple.com",
        },
      } });
      throw new Error(`unexpected ${url}`);
    });
    renderDES("AAPL");
    expect(await screen.findByText("Apple Inc.")).toBeInTheDocument();
    expect(screen.getByText("NasdaqGS")).toBeInTheDocument();
    expect(screen.getByText("Technology")).toBeInTheDocument();
    expect(screen.getByText("4.67T")).toBeInTheDocument();
    expect(screen.getByText("36.61")).toBeInTheDocument();
    expect(screen.getByText("0.34%")).toBeInTheDocument();
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
});

describe("SessionProvider", () => {
  it("settles on unauthenticated when the initial /me call rejects", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("network"));
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <SessionProvider>
        <SessionProbe />
      </SessionProvider>,
    );
    expect(await screen.findByText("session:false")).toBeInTheDocument();
    expect(errorSpy).toHaveBeenCalledWith("session refresh failed", expect.any(TypeError));
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
