// CA: the dividend chart's arithmetic, and that it sits above the table
// rather than instead of it.
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CA, CA_PANEL, MAX_YEARS, dividendChart } from "./CA";
import { Layout } from "../commands/types";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const dividend = (date: string, value: string) => ({
  symbol: "AAPL",
  action_date: date,
  action_type: "DIVIDEND",
  action_value: value,
});

const split = (date: string, ratio: string) => ({
  symbol: "AAPL",
  action_date: date,
  action_type: "SPLIT",
  action_value: ratio,
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("CA_PANEL", () => {
  it("is a single-layout panel that needs a symbol", () => {
    expect(CA_PANEL.layout).toBe(Layout.Single);
    expect(CA_PANEL.needsSymbol).toBe(true);
  });
});

describe("dividendChart", () => {
  it("sums the payments of a year into one bar", () => {
    // A quarterly payer has four payments a year and one answer: is the
    // payout growing.
    const chart = dividendChart([
      dividend("2025-02-13", "0.25"),
      dividend("2025-05-12", "0.25"),
      dividend("2025-08-11", "0.26"),
      dividend("2025-11-10", "0.26"),
      dividend("2024-02-15", "0.24"),
    ]);
    expect(chart?.years).toEqual(["2024", "2025"]);
    expect(chart?.amounts).toEqual([0.24, 1.02]);
  });

  it("puts a split on the axis rather than in a bar", () => {
    // A split has no amount; on the value scale it would claim one.
    const chart = dividendChart([dividend("2020-08-07", "0.20"), split("2020-08-31", "4.0")]);
    expect(chart?.marks).toEqual([{ category: "2020", label: "4-for-1 split" }]);
    expect(chart?.amounts).toEqual([0.2]);
  });

  it("drops a split from a year the axis does not reach", () => {
    const years = Array.from({ length: MAX_YEARS + 3 }, (_, i) =>
      dividend(`${2000 + i}-06-01`, "1"),
    );
    const chart = dividendChart([...years, split("2000-06-15", "2.0")]);
    expect(chart?.years).toHaveLength(MAX_YEARS);
    expect(chart?.years[0]).toBe("2003");
    expect(chart?.marks).toEqual([]);
  });

  it("is null for a symbol that has never paid one", () => {
    expect(dividendChart([split("2020-08-31", "4.0")])).toBeNull();
    expect(dividendChart([])).toBeNull();
  });
});

describe("CA", () => {
  it("draws the chart above the table of actions", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      json({ data: [dividend("2025-02-13", "0.25"), split("2020-08-31", "4.0")], next_cursor: null }),
    );
    render(<CA symbol="AAPL" args={{}} />);
    const chart = await screen.findByRole("img", { name: /dividends per year/ });
    const table = screen.getByRole("table");
    expect(chart.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("shows the table alone when there are no dividends to chart", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      json({ data: [split("2020-08-31", "4.0")], next_cursor: null }),
    );
    render(<CA symbol="AAPL" args={{}} />);
    await screen.findByRole("table");
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });
});
