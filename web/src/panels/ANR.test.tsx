import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ANR, ANR_PANEL } from "./ANR";
import { SessionProvider, useSession } from "../app/session";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function problem(status: number, type: string): Response {
  return new Response(JSON.stringify({ type, title: type }), {
    status,
    headers: { "content-type": "application/problem+json" },
  });
}

const me = { authenticated: true, expires_at: 1, live_enabled: false };

function page(rows: unknown[]) {
  return json(200, { data: rows, next_cursor: null, as_of: null });
}

const URLS = {
  targets: "/ui/api/v1/datasets/analyst_price_targets?symbol=AAPL&limit=200",
  recommendations: "/ui/api/v1/datasets/recommendations?symbol=AAPL&limit=200",
  grades: "/ui/api/v1/datasets/upgrades_downgrades?symbol=AAPL&limit=200",
  estimates: "/ui/api/v1/datasets/earnings_estimate?symbol=AAPL&limit=200",
  trend: "/ui/api/v1/datasets/eps_trend?symbol=AAPL&limit=200",
};

const targets = [{ symbol: "AAPL", as_of_date: "2026-09-01", current: "319.97", low: "200", high: "400", mean: "310.5", median: "312" }];
const grades = [
  { symbol: "AAPL", grade_ts_utc: "2026-08-30T12:00:00Z", firm: "Morgan Stanley", from_grade: "Equal-Weight", to_grade: "Overweight", action: "up" },
];
const estimates = [
  { symbol: "AAPL", as_of_date: "2026-09-01", metric: "eps", period: "0q", avg: "1.61", low: "1.50", high: "1.70", number_of_analysts: 28, growth: "0.12" },
  { symbol: "AAPL", as_of_date: "2026-08-01", metric: "eps", period: "0q", avg: "1.55", low: "1.40", high: "1.65", number_of_analysts: 27, growth: "0.10" },
];
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function SessionProbe() {
  const { me } = useSession();
  return <span>session:{String(me?.authenticated ?? "null")}</span>;
}

function renderANR() {
  return render(
    <SessionProvider>
      <SessionProbe />
      <ANR symbol="AAPL" args={{}} />
    </SessionProvider>,
  );
}

describe("ANR_PANEL", () => {
  it("is a single-layout panel that needs a symbol and takes no args", () => {
    expect(ANR_PANEL.code).toBe("ANR");
    expect(ANR_PANEL.layout).toBe("single");
    expect(ANR_PANEL.needsSymbol).toBe(true);
    expect(ANR_PANEL.parseArgs(["anything"])).toEqual({});
  });
});

describe("ANR", () => {
  it("renders every section independently: tables, an empty note and an error card", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/ui/api/me") return json(200, me);
      if (url === URLS.targets) return page(targets);
      if (url === URLS.recommendations) return page([]);
      if (url === URLS.grades) return page(grades);
      if (url === URLS.estimates) return page(estimates);
      if (url === URLS.trend) return problem(500, "internal_error");
      throw new Error(`unexpected ${url}`);
    });
    renderANR();
    // Price targets summary row.
    expect(await screen.findByText("319.97")).toBeInTheDocument();
    expect(screen.getByText("312.00")).toBeInTheDocument();
    // Grades: from -> to in one cell.
    expect(screen.getByText("Morgan Stanley")).toBeInTheDocument();
    expect(screen.getByText("Equal-Weight → Overweight")).toBeInTheDocument();
    // Estimates: only the newest as_of_date's rows are shown.
    expect(screen.getByText("1.61")).toBeInTheDocument();
    expect(screen.queryByText("1.55")).not.toBeInTheDocument();
    // Empty section says so; failed section offers Retry without hiding the rest.
    expect(screen.getByText("No recommendations.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  it("shows one empty card when every section is empty", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/ui/api/me") return json(200, me);
      if (url.startsWith("/ui/api/v1/datasets/")) return page([]);
      throw new Error(`unexpected ${url}`);
    });
    renderANR();
    expect(await screen.findByText("No analyst data for this symbol.")).toBeInTheDocument();
  });

  it("drops the session when any section answers 401", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/ui/api/me") return json(200, me);
      if (url === URLS.grades) return problem(401, "unauthenticated");
      if (url.startsWith("/ui/api/v1/datasets/")) return page([]);
      throw new Error(`unexpected ${url}`);
    });
    renderANR();
    await screen.findByText("session:true");
    expect(await screen.findByText("session:false")).toBeInTheDocument();
  });
});
