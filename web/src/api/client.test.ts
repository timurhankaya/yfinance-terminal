import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  Interval,
  PAGE_LIMIT,
  PAGE_SIZE,
  apiFetch,
  getActions,
  getBars,
  getCatalog,
  getDataset,
  getDatasetPage,
  getFinancials,
  getSymbol,
  resetCatalogCache,
  searchSymbols,
} from "./client";

afterEach(() => vi.restoreAllMocks());

function respond(status: number, body: unknown, contentType = "application/json"): Response {
  return new Response(body === null ? null : JSON.stringify(body), {
    status,
    headers: { "content-type": contentType },
  });
}

describe("apiFetch", () => {
  afterEach(() => vi.restoreAllMocks());

  it("sends same-origin credentials and never caches", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(respond(200, { ok: 1 }));
    await apiFetch("/ui/api/v1/datasets");
    const [, init] = spy.mock.calls[0]!;
    expect(init?.credentials).toBe("same-origin");
    expect(init?.cache).toBe("no-store");
  });

  it("turns a 401 into an ordinary ApiError, with no state of its own", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      respond(401, { type: "unauthenticated", title: "x" }, "application/problem+json"),
    );
    const err = await apiFetch("/ui/api/v1/symbols/AAPL").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(401);
    expect((err as ApiError).type).toBe("unauthenticated");
  });

  it("turns another problem into ApiError with the type", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      respond(404, { type: "not_found", title: "No such symbol" }, "application/problem+json"),
    );
    const err = await apiFetch("/ui/api/v1/symbols/NOPE").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(404);
    expect((err as ApiError).type).toBe("not_found");
  });

  it("returns undefined for a 204", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 204 }));
    await expect(apiFetch("/ui/api/v1/datasets", { method: "POST" })).resolves.toBeUndefined();
  });
});

describe("endpoints", () => {
  afterEach(() => vi.restoreAllMocks());

  it("getSymbol upper-cases and unwraps the resource envelope", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      respond(200, { data: { symbol: "AAPL", is_active: true, info: null } }),
    );
    const detail = await getSymbol("aapl");
    expect(spy.mock.calls[0]![0]).toBe("/ui/api/v1/symbols/AAPL");
    expect(detail.symbol).toBe("AAPL");
  });

  it("searchSymbols skips the fetch entirely for a too-short prefix", async () => {
    const spy = vi.spyOn(globalThis, "fetch");
    await expect(searchSymbols(" ")).resolves.toEqual([]);
    expect(spy).not.toHaveBeenCalled();
  });

  it("getFinancials follows next_cursor and concatenates the pages", async () => {
    const spy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        respond(200, {
          data: [{ period_end: "2024-01-01", item_key: "revenue", value: "1", currency: "USD" }],
          next_cursor: "page2",
        }),
      )
      .mockResolvedValueOnce(
        respond(200, {
          data: [{ period_end: "2024-04-01", item_key: "revenue", value: "2", currency: "USD" }],
          next_cursor: null,
        }),
      );
    const rows = await getFinancials("aapl", "income", "quarterly");
    expect(rows).toHaveLength(2);
    expect(rows[1]?.value).toBe("2");
    const secondUrl = spy.mock.calls[1]![0] as string;
    expect(secondUrl).toContain("cursor=page2");
  });

  it("getFinancials stops after exactly 5 fetches when next_cursor never runs out", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      const pageNum = /cursor=page(\d)/.exec(url)?.[1] ?? "0";
      return Promise.resolve(
        respond(200, {
          data: [{ period_end: `2024-0${pageNum}-01`, item_key: "revenue", value: pageNum, currency: "USD" }],
          next_cursor: `page${Number(pageNum) + 1}`,
        }),
      );
    });
    const rows = await getFinancials("aapl", "income", "quarterly");
    expect(spy).toHaveBeenCalledTimes(5);
    expect(rows).toHaveLength(5);
    expect(rows.map((r) => r.value)).toEqual(["0", "1", "2", "3", "4"]);
  });

  it("getDataset carries symbol, limit and extra params in the query", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(respond(200, { data: [], next_cursor: null }));
    await getDataset("recommendations", "aapl", { filing_type: "10-K" });
    const url = spy.mock.calls[0]![0] as string;
    expect(url).toContain("symbol=AAPL");
    expect(url).toContain("limit=200");
    expect(url).toContain("filing_type=10-K");
  });

  it("getDataset's symbol and limit always win over caller-supplied params of the same name", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(respond(200, { data: [], next_cursor: null }));
    await getDataset("recommendations", "aapl", { symbol: "msft", limit: "5", filing_type: "10-K" });
    const url = spy.mock.calls[0]![0] as string;
    expect(url).toContain("symbol=AAPL");
    expect(url).toContain("limit=200");
    expect(url).toContain("filing_type=10-K");
    expect(url).not.toContain("symbol=MSFT");
    expect(url).not.toContain("limit=5");
  });
});

describe("catalogue and dataset rows", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    resetCatalogCache();
  });

  it("getCatalog fetches once and caches the entries", async () => {
    const entry = { name: "major_holders", family: "holders", symbol_scoped: true, filters: [], columns: [] };
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(respond(200, { data: [entry], next_cursor: null }));
    const first = await getCatalog();
    const second = await getCatalog();
    expect(first).toEqual(second);
    expect(first[0]?.name).toBe("major_holders");
    expect(spy).toHaveBeenCalledTimes(1);
    expect(spy.mock.calls[0]![0]).toBe("/ui/api/v1/datasets");
  });

  it("getCatalog does not cache a failure", async () => {
    const spy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(respond(500, { type: "internal" }, "application/problem+json"))
      .mockResolvedValueOnce(respond(200, { data: [], next_cursor: null }));
    await expect(getCatalog()).rejects.toBeInstanceOf(ApiError);
    await expect(getCatalog()).resolves.toEqual([]);
    expect(spy).toHaveBeenCalledTimes(2);
  });

  it("getDatasetPage carries the filters and the page size", async () => {
    const spy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(respond(200, { data: [{ a: 1 }], next_cursor: null }));
    const page = await getDatasetPage("screens", { kind: "predefined" });
    expect(spy.mock.calls[0]![0]).toBe(
      `/ui/api/v1/datasets/screens?kind=predefined&limit=${PAGE_SIZE}`,
    );
    expect(page).toEqual({ rows: [{ a: 1 }], next_cursor: null });
  });

  it("getDatasetPage hands the cursor back so the caller can continue", async () => {
    // Paging is the CALLER's, not this function's: a panel decides how
    // far to walk, and the load-more button is what asks for the next
    // page. Following cursors here would fetch rows nobody scrolled to.
    const spy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(respond(200, { data: [{ a: 2 }], next_cursor: "p2" }));
    const page = await getDatasetPage("screens", {}, "p1", 10);
    expect(spy.mock.calls[0]![0]).toBe("/ui/api/v1/datasets/screens?limit=10&cursor=p1");
    expect(page.next_cursor).toBe("p2");
  });

  it("getActions and getBars address the symbol routes", async () => {
    const spy = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(() => Promise.resolve(respond(200, { data: [], next_cursor: null })));
    await getActions("aapl");
    const now = Date.parse("2026-09-07T00:00:00Z");
    await getBars("aapl", Interval.D1, 50, now);
    expect(spy.mock.calls[0]![0]).toBe(`/ui/api/v1/symbols/AAPL/actions?limit=${PAGE_LIMIT}`);
    // 50 daily bars: 80 calendar days back, one full page, the tail kept.
    const from = encodeURIComponent(new Date(now - 1.6 * 86_400_000 * 50).toISOString());
    expect(spy.mock.calls[1]![0]).toBe(`/ui/api/v1/symbols/AAPL/bars?interval=1d&from=${from}&limit=${PAGE_LIMIT}`);
  });

  it("getBars keeps only the newest rows of what the pages returned", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(() =>
      Promise.resolve(respond(200, { data: [{ n: 1 }, { n: 2 }, { n: 3 }], next_cursor: null })),
    );
    await expect(getBars("aapl", Interval.D1, 2)).resolves.toEqual([{ n: 2 }, { n: 3 }]);
  });
});

it("resolves a single-letter ticker through its exact symbol endpoint", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(respond(200, { data: { symbol: "F", long_name: "Ford Motor Company" } }));
  expect(await searchSymbols("f")).toEqual([{ symbol: "F", long_name: "Ford Motor Company" }]);
  expect(fetch).toHaveBeenCalledWith("/ui/api/v1/symbols/F", expect.anything());
});

it("treats an unknown single-letter ticker as an empty search, while preserving server errors", async () => {
  const fetcher = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(respond(404, { title: "Not found" })).mockResolvedValueOnce(respond(503, { title: "Unavailable" }));
  expect(await searchSymbols("X")).toEqual([]);
  await expect(searchSymbols("X")).rejects.toThrow();
  expect(fetcher).toHaveBeenCalledTimes(2);
});
