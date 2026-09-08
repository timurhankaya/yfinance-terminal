import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Layout } from "../commands/types";
import { ANR, ANR_PANEL, recommendationBars, targetRange } from "./ANR";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function problem(status: number, type: string): Response {
  return new Response(JSON.stringify({ type, title: type }), {
    status,
    headers: { "content-type": "application/problem+json" },
  });
}


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

function renderANR() {
  return render(<ANR symbol="AAPL" args={{}} />);
}

describe("ANR_PANEL", () => {
  it("is a single-layout panel that needs a symbol and takes no args", () => {
    expect(ANR_PANEL.code).toBe("ANR");
    expect(ANR_PANEL.layout).toBe(Layout.Single);
    expect(ANR_PANEL.needsSymbol).toBe(true);
    expect(ANR_PANEL.parseArgs(["anything"])).toEqual({});
  });
});

describe("ANR", () => {
  it("renders every section independently: tables, an empty note and an error card", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
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
      if (url.startsWith("/ui/api/v1/datasets/")) return page([]);
      throw new Error(`unexpected ${url}`);
    });
    renderANR();
    expect(await screen.findByText("No analyst data for this symbol.")).toBeInTheDocument();
  });

  it("keeps a 401 inside its own section instead of failing the panel", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === URLS.grades) return problem(401, "unauthenticated");
      if (url === URLS.targets) return page(targets);
      if (url.startsWith("/ui/api/v1/datasets/")) return page([]);
      throw new Error(`unexpected ${url}`);
    });
    renderANR();
    // The other sections still render; only the 401 one shows a retryable card.
    expect(await screen.findByText("319.97")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });
});


describe("targetRange", () => {
  it("reads the newest row as a range with the price on it", () => {
    const range = targetRange(targets);
    expect(range).toEqual({ low: 200, high: 400, mean: 310.5, actual: 319.97 });
  });

  it("is null unless low, mean and high are all there and in order", () => {
    // Three numbers about one thing is what makes the picture; a range
    // drawn from two of them would be a different claim.
    expect(targetRange([{ low: "200", high: "400" }])).toBeNull();
    expect(targetRange([{ low: "400", high: "200", mean: "300" }])).toBeNull();
    expect(targetRange([])).toBeNull();
  });

  it("keeps a range whose current price is unknown", () => {
    expect(targetRange([{ low: "1", mean: "2", high: "3" }])?.actual).toBeNull();
  });
});

describe("recommendationBars", () => {
  const rows = [
    { as_of_date: "2026-09-01", period: "0m", strong_buy: 10, buy: 8, hold: 3, sell: 1, strong_sell: 0 },
    { as_of_date: "2026-09-01", period: "-2m", strong_buy: 8, buy: 9, hold: 4, sell: 1, strong_sell: 0 },
    { as_of_date: "2026-09-01", period: "-1m", strong_buy: 9, buy: 9, hold: 3, sell: 1, strong_sell: 0 },
    { as_of_date: "2026-08-01", period: "0m", strong_buy: 1, buy: 1, hold: 1, sell: 1, strong_sell: 1 },
  ];

  it("takes the newest snapshot only, oldest period first", () => {
    // Sorted as the number the period key is: as text, `-1m` would sort
    // before `-2m`.
    const bars = recommendationBars(rows);
    expect(bars?.categories).toEqual(["-2m", "-1m", "0m"]);
    expect(bars?.series[0]?.values).toEqual([8, 9, 10]);
  });

  it("has one series per rating bucket, strongest first", () => {
    expect(recommendationBars(rows)?.series.map((one) => one.key)).toEqual([
      "strong_buy",
      "buy",
      "hold",
      "sell",
      "strong_sell",
    ]);
  });

  it("is null when nothing was counted, or there is nothing to count", () => {
    expect(recommendationBars([{ as_of_date: "2026-09-01", period: "0m" }])).toBeNull();
    expect(recommendationBars([])).toBeNull();
  });
});

describe("ANR's charts", () => {
  const recommendations = [
    { symbol: "AAPL", as_of_date: "2026-09-01", period: "0m", strong_buy: 10, buy: 8, hold: 3, sell: 1, strong_sell: 0 },
  ];

  function stubAll() {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === URLS.targets) return page(targets);
      if (url === URLS.recommendations) return page(recommendations);
      return page([]);
    });
  }

  it("draws the target range and the distribution above their tables", async () => {
    stubAll();
    renderANR();
    const bullet = await screen.findByRole("img", { name: /price target: 200.00 to 400.00/ });
    const bars = screen.getByRole("img", { name: /recommendations by period/ });
    expect(bullet).toBeInTheDocument();
    expect(bars).toBeInTheDocument();
    const firstTable = screen.getAllByRole("table")[0];
    expect(
      bullet.compareDocumentPosition(firstTable as Node) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("draws no chart for a section the archive has nothing for", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) =>
      String(input) === URLS.targets ? page(targets) : page([]),
    );
    renderANR();
    await screen.findByRole("img", { name: /price target/ });
    expect(screen.queryByRole("img", { name: /recommendations by period/ })).not.toBeInTheDocument();
  });
});
