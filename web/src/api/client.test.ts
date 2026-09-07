import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  UnauthorizedError,
  apiFetch,
  getDataset,
  getFinancials,
  getMe,
  getSymbol,
  login,
  searchSymbols,
} from "./client";

const PW = "hunter2";

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
    await apiFetch("/ui/api/me");
    const [, init] = spy.mock.calls[0]!;
    expect(init?.credentials).toBe("same-origin");
    expect(init?.cache).toBe("no-store");
  });

  it("turns a 401 into UnauthorizedError", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      respond(401, { type: "unauthenticated", title: "x" }, "application/problem+json"),
    );
    await expect(apiFetch("/ui/api/v1/symbols/AAPL")).rejects.toBeInstanceOf(UnauthorizedError);
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
    await expect(apiFetch("/ui/api/logout", { method: "POST" })).resolves.toBeUndefined();
  });
});

describe("endpoints", () => {
  afterEach(() => vi.restoreAllMocks());

  it("getMe reads /ui/api/me", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      respond(200, { authenticated: false, expires_at: null, live_enabled: false }),
    );
    const me = await getMe();
    expect(spy.mock.calls[0]![0]).toBe("/ui/api/me");
    expect(me.authenticated).toBe(false);
  });

  it("login posts the form field", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 204 }));
    await login(PW);
    const [url, init] = spy.mock.calls[0]!;
    expect(url).toBe("/ui/api/login");
    expect(init?.method).toBe("POST");
    expect(String(init?.body)).toContain(`password=${PW}`);
  });

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
    await expect(searchSymbols("a")).resolves.toEqual([]);
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
