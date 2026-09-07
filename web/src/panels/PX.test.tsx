import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CA } from "./CA";
import { PX, PX_PANEL, PX_USAGE } from "./PX";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}


afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("PX parseArgs", () => {
  it("takes an interval and a row count within the page cap", () => {
    expect(PX_PANEL.parseArgs([])).toEqual({ interval: "1d", rows: "250" });
    expect(PX_PANEL.parseArgs(["5m", "50"])).toEqual({ interval: "5m", rows: "50" });
    expect(() => PX_PANEL.parseArgs(["2m"])).toThrow(PX_USAGE);
    expect(() => PX_PANEL.parseArgs(["1d", "0"])).toThrow(PX_USAGE);
    expect(() => PX_PANEL.parseArgs(["1d", "1001"])).toThrow(PX_USAGE);
    expect(() => PX_PANEL.parseArgs(["1d", "ten"])).toThrow(PX_USAGE);
  });

  it("brings a hand-edited URL back into range before the panel runs", () => {
    // Args reach a panel from a bookmarked URL too, and `normalizeArgs`
    // is the one place that is settled -- the panel body reads them as
    // given rather than re-checking each one.
    expect(PX_PANEL.normalizeArgs?.({ interval: "2m", rows: "9999" })).toEqual({
      interval: "1d",
      rows: "250",
    });
    expect(PX_PANEL.normalizeArgs?.({})).toEqual({ interval: "1d", rows: "250" });
    expect(PX_PANEL.normalizeArgs?.({ interval: "5m", rows: "50" })).toEqual({
      interval: "5m",
      rows: "50",
    });
  });
});

describe("PX", () => {
  it("shows bars newest first with the symbol column hidden", async () => {
    const seen: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      seen.push(url);
      return json(200, {
        data: [
          { symbol: "AAPL", ts_utc: "2026-09-03T04:00:00Z", open: "1", high: "2", low: "0.5", close: "1.5", adj_close: "1.5", volume: 100, session_date: "2026-09-03", bar_interval: null, local_date: null, is_extended: null },
          { symbol: "AAPL", ts_utc: "2026-09-04T04:00:00Z", open: "2", high: "3", low: "1.5", close: "2.5", adj_close: "2.5", volume: 200, session_date: "2026-09-04", bar_interval: null, local_date: null, is_extended: null },
        ],
        next_cursor: null,
      });
    });
    render(
      <PX symbol="AAPL" args={{ interval: "1d", rows: "2" }} />,
    );
    const rows = await screen.findAllByRole("row");
    expect(rows[1]!.textContent).toContain("2026-09-04");
    expect(seen.some((u) => u.startsWith("/ui/api/v1/symbols/AAPL/bars?interval=1d&from=") && u.endsWith("&limit=1000"))).toBe(true);
    expect(screen.getAllByRole("columnheader").map((th) => th.textContent)).not.toContain("symbol");
  });
});

describe("CA", () => {
  it("shows actions newest first, and an empty card when there are none", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/symbols/AAPL/actions")) {
        return json(200, {
          data: [
            { symbol: "AAPL", action_date: "1987-05-11", action_type: "DIVIDEND", action_value: "0.000536" },
            { symbol: "AAPL", action_date: "1987-06-16", action_type: "SPLIT", action_value: "2" },
          ],
          next_cursor: null,
        });
      }
      return json(200, { data: [], next_cursor: null });
    });
    const first = render(
      <CA symbol="AAPL" args={{}} />,
    );
    const rows = await screen.findAllByRole("row");
    expect(rows[1]!.textContent).toContain("SPLIT");
    first.unmount();

    render(
      <CA symbol="MSFT" args={{}} />,
    );
    expect(await screen.findByText(/No corporate actions/)).toBeInTheDocument();
  });
});
