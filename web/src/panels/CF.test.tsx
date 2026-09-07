import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Layout } from "../commands/types";
import { CF, CF_PANEL } from "./CF";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}


const filings = [
  { symbol: "AAPL", filing_id: "0000320193-25-000079", filing_date: "2025-11-01", filing_type: "10-K", title: "Annual report", edgar_url: "https://finance.yahoo.com/sec-filing/AAPL/0000320193-25-000079_320193", exhibit_count: 2 },
  { symbol: "AAPL", filing_id: "0000320193-25-000050", filing_date: "2025-08-01", filing_type: "10-Q", title: null, edgar_url: "https://finance.yahoo.com/sec-filing/AAPL/0000320193-25-000050_320193", exhibit_count: 0 },
];
const exhibits = [
  { symbol: "AAPL", filing_id: "0000320193-25-000079", exhibit_type: "EX-31.1", url_hash: "h1", url: "https://sec.gov/a/ex31" },
  { symbol: "AAPL", filing_id: "0000320193-25-000079", exhibit_type: "EX-32.1", url_hash: "h2", url: "https://sec.gov/a/ex32" },
];

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function renderCF(args: Record<string, string> = {}) {
  const spy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    if (url.startsWith("/ui/api/v1/datasets/sec_filings?")) return json(200, { data: filings, next_cursor: null, as_of: null });
    if (url.startsWith("/ui/api/v1/datasets/sec_filing_exhibits?")) return json(200, { data: exhibits, next_cursor: null, as_of: null });
    throw new Error(`unexpected ${url}`);
  });
  render(
    <CF symbol="AAPL" args={args} />,
  );
  return spy;
}

describe("CF_PANEL.parseArgs", () => {
  it("upper-cases a filing type and accepts none", () => {
    expect(CF_PANEL.parseArgs(["10-k"])).toEqual({ filing_type: "10-K" });
    expect(CF_PANEL.parseArgs([])).toEqual({});
    expect(CF_PANEL.layout).toBe(Layout.Single);
    expect(CF_PANEL.needsSymbol).toBe(true);
  });

  it("publishes its argument syntax so HELP can show it", () => {
    expect(CF_PANEL.usage).toBe("CF [filing type]");
  });
});

describe("CF", () => {
  it("lists filings and expands exhibits with Enter", async () => {
    renderCF();
    expect(await screen.findByText("Annual report")).toBeInTheDocument();
    expect(screen.getByText("10-Q")).toBeInTheDocument();
    const edgarLinks = screen.getAllByRole("link", { name: /EDGAR/ });
    expect(edgarLinks).toHaveLength(2);
    expect(edgarLinks[0]).toHaveAttribute(
      "href",
      "https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/",
    );
    expect(screen.queryByText("EX-31.1")).not.toBeInTheDocument();

    await userEvent.keyboard("{Enter}");
    expect(screen.getByText("EX-31.1")).toBeInTheDocument();
    expect(screen.getByText("EX-32.1")).toBeInTheDocument();
    // In the right-hand pane, not inside the row.
    const pane = screen.getByRole("article", { name: "exhibits" });
    expect(pane.closest(".split-detail")).not.toBeNull();
    expect(pane.closest("li")).toBeNull();

    // Enter again collapses; j then Enter opens the second filing, which has no exhibits.
    await userEvent.keyboard("{Enter}");
    expect(screen.queryByText("EX-31.1")).not.toBeInTheDocument();
    await userEvent.keyboard("j");
    await userEvent.keyboard("{Enter}");
    expect(screen.getByText(/No exhibits fetched for this filing/)).toBeInTheDocument();
  });

  it("passes the filing type filter through to the API", async () => {
    const spy = renderCF({ filing_type: "10-K" });
    await screen.findByText("Annual report");
    const filingsUrl = spy.mock.calls.map(([u]) => String(u)).find((u) => u.startsWith("/ui/api/v1/datasets/sec_filings?"));
    expect(filingsUrl).toContain("filing_type=10-K");
    expect(filingsUrl).toContain("symbol=AAPL");
  });

  it("appends the next page of filings with the cursor", async () => {
    const seen: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      seen.push(url);
      if (url.startsWith("/ui/api/v1/datasets/sec_filing_exhibits?")) {
        return json(200, { data: exhibits, next_cursor: null, as_of: null });
      }
      if (url.includes("cursor=c1")) {
        return json(200, {
          data: [{ ...filings[0]!, filing_id: "0000320193-24-000001", title: "Older report" }],
          next_cursor: null,
          as_of: null,
        });
      }
      return json(200, { data: filings, next_cursor: "c1", as_of: null });
    });
    render(<CF symbol="AAPL" args={{}} />);
    await screen.findByText("Annual report");
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByText("Older report")).toBeInTheDocument();
    expect(screen.getByText("Annual report")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
    expect(seen.some((u) => u.includes("cursor=c1"))).toBe(true);
  });

  it("says why the next page failed instead of a button that does nothing", async () => {
    // The failure used to be an unhandled rejection in the console and
    // nothing at all on screen.
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.startsWith("/ui/api/v1/datasets/sec_filing_exhibits?")) {
        return json(200, { data: exhibits, next_cursor: null, as_of: null });
      }
      if (url.includes("cursor=c1")) {
        return new Response(JSON.stringify({ type: "internal", title: "Server error" }), {
          status: 500,
          headers: { "content-type": "application/problem+json" },
        });
      }
      return json(200, { data: filings, next_cursor: "c1", as_of: null });
    });
    render(<CF symbol="AAPL" args={{}} />);
    await screen.findByText("Annual report");
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByText(/Could not load the next page: Server error/)).toBeInTheDocument();
    // The first page is still there, and the button still offers a retry.
    expect(screen.getByText("Annual report")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Load more" })).toBeInTheDocument();
  });

  it("shows an empty card when there are no filings", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.startsWith("/ui/api/v1/datasets/")) return json(200, { data: [], next_cursor: null, as_of: null });
      throw new Error(`unexpected ${url}`);
    });
    render(
      <CF symbol="AAPL" args={{}} />,
    );
    expect(await screen.findByText("No filings for this symbol.")).toBeInTheDocument();
  });
});
