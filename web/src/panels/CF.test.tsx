import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CF, CF_PANEL } from "./CF";
import { SessionProvider } from "../app/session";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

const me = { authenticated: true, expires_at: 1, live_enabled: false };

const filings = [
  { symbol: "AAPL", filing_id: "0000320193-25-000079", filing_date: "2025-11-01", filing_type: "10-K", title: "Annual report", edgar_url: "https://sec.gov/a", exhibit_count: 2 },
  { symbol: "AAPL", filing_id: "0000320193-25-000050", filing_date: "2025-08-01", filing_type: "10-Q", title: null, edgar_url: "https://sec.gov/b", exhibit_count: 0 },
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
    if (url === "/ui/api/me") return json(200, me);
    if (url.startsWith("/ui/api/v1/datasets/sec_filings?")) return json(200, { data: filings, next_cursor: null, as_of: null });
    if (url.startsWith("/ui/api/v1/datasets/sec_filing_exhibits?")) return json(200, { data: exhibits, next_cursor: null, as_of: null });
    throw new Error(`unexpected ${url}`);
  });
  render(
    <SessionProvider>
      <CF symbol="AAPL" args={args} />
    </SessionProvider>,
  );
  return spy;
}

describe("CF_PANEL.parseArgs", () => {
  it("upper-cases a filing type and accepts none", () => {
    expect(CF_PANEL.parseArgs(["10-k"])).toEqual({ filing_type: "10-K" });
    expect(CF_PANEL.parseArgs([])).toEqual({});
    expect(CF_PANEL.layout).toBe("single");
    expect(CF_PANEL.needsSymbol).toBe(true);
  });
});

describe("CF", () => {
  it("lists filings and expands exhibits with Enter", async () => {
    renderCF();
    expect(await screen.findByText("Annual report")).toBeInTheDocument();
    expect(screen.getByText("10-Q")).toBeInTheDocument();
    const edgarLinks = screen.getAllByRole("link", { name: /EDGAR/ });
    expect(edgarLinks).toHaveLength(2);
    expect(edgarLinks[0]).toHaveAttribute("href", "https://sec.gov/a");
    expect(screen.queryByText("EX-31.1")).not.toBeInTheDocument();

    await userEvent.keyboard("{Enter}");
    expect(screen.getByText("EX-31.1")).toBeInTheDocument();
    expect(screen.getByText("EX-32.1")).toBeInTheDocument();

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

  it("shows an empty card when there are no filings", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/ui/api/me") return json(200, me);
      if (url.startsWith("/ui/api/v1/datasets/")) return json(200, { data: [], next_cursor: null, as_of: null });
      throw new Error(`unexpected ${url}`);
    });
    render(
      <SessionProvider>
        <CF symbol="AAPL" args={{}} />
      </SessionProvider>,
    );
    expect(await screen.findByText("No filings for this symbol.")).toBeInTheDocument();
  });
});
