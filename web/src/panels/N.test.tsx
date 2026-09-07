import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { N, N_PANEL } from "./N";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}


const articles = [
  {
    news_id: "n-newer", title: "Apple unveils something", summary: "The newer story.",
    pub_date: "2026-09-06T14:00:00Z", provider_name: "Reuters", link: "https://example.com/newer", thumbnail_url: null,
  },
  {
    news_id: "n-older", title: "Apple did a thing", summary: null,
    pub_date: "2026-09-05T09:30:00Z", provider_name: null, link: "https://example.com/older", thumbnail_url: null,
  },
];

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function renderN(rows: unknown[]) {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    if (url === "/ui/api/symbols/AAPL/news") return json(200, { data: rows, next_cursor: null, as_of: null });
    throw new Error(`unexpected ${url}`);
  });
  return render(
    <N symbol="AAPL" args={{}} />,
  );
}

describe("N_PANEL", () => {
  it("is a single-layout panel that needs a symbol and takes no args", () => {
    expect(N_PANEL.code).toBe("N");
    expect(N_PANEL.layout).toBe("single");
    expect(N_PANEL.needsSymbol).toBe(true);
    expect(N_PANEL.parseArgs(["x"])).toEqual({});
  });
});

describe("N", () => {
  it("lists articles and opens the one chosen with j and Enter", async () => {
    renderN(articles);
    expect(await screen.findByText("Apple unveils something")).toBeInTheDocument();
    expect(screen.getByText("Reuters")).toBeInTheDocument();
    // Nothing is open until the user picks a row.
    expect(screen.queryByRole("link", { name: /^Open article/ })).not.toBeInTheDocument();

    await userEvent.keyboard("j");
    await userEvent.keyboard("{Enter}");

    expect(screen.getByText("No summary.")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /^Open article/ });
    expect(link).toHaveAttribute("href", "https://example.com/older");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("opens an article on click and shows its summary", async () => {
    renderN(articles);
    await userEvent.click(await screen.findByText("Apple unveils something"));
    expect(screen.getByText("The newer story.")).toBeInTheDocument();
  });

  it("shows an empty card when there is no news", async () => {
    renderN([]);
    expect(await screen.findByText("No news for this symbol.")).toBeInTheDocument();
  });
});
