import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, UnauthorizedError, apiFetch, getMe, getSymbol, login } from "./client";

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
    await expect(apiFetch("/v1/symbols/AAPL")).rejects.toBeInstanceOf(UnauthorizedError);
  });

  it("turns another problem into ApiError with the type", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      respond(404, { type: "not_found", title: "No such symbol" }, "application/problem+json"),
    );
    const err = await apiFetch("/v1/symbols/NOPE").catch((e: unknown) => e);
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
    expect(spy.mock.calls[0]![0]).toBe("/v1/symbols/AAPL");
    expect(detail.symbol).toBe("AAPL");
  });
});
