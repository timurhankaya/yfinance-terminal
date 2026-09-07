import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { SessionProvider, useSession } from "../app/session";
import { useListKeys, usePanelData } from "./common";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

const me = { authenticated: true, expires_at: 1, live_enabled: false };

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function wrapper({ children }: { children: ReactNode }) {
  return <SessionProvider>{children}</SessionProvider>;
}

describe("usePanelData", () => {
  it("does not load while unauthenticated, then loads once the session refreshes", async () => {
    let authed = false;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/ui/api/me") return json(200, { ...me, authenticated: authed });
      throw new Error(`unexpected ${url}`);
    });
    const load = vi.fn(async () => "data");
    const { result } = renderHook(
      () => {
        const session = useSession();
        const panel = usePanelData("k", load);
        return { session, panel };
      },
      { wrapper },
    );
    await waitFor(() => expect(result.current.session.me).not.toBeNull());
    expect(result.current.panel.state).toEqual({ kind: "loading" });
    expect(load).not.toHaveBeenCalled();

    authed = true;
    await act(async () => {
      await result.current.session.refresh();
    });
    await waitFor(() => expect(result.current.panel.state).toEqual({ kind: "ready", data: "data" }));
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("drops a stale response when the key changes before it resolves", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/ui/api/me") return json(200, me);
      throw new Error(`unexpected ${url}`);
    });
    let resolveFirst!: (value: string) => void;
    const first = new Promise<string>((resolve) => {
      resolveFirst = resolve;
    });
    const load = vi.fn(() => first);
    const { result, rerender } = renderHook(
      ({ key }: { key: string }) => usePanelData(key, load),
      { wrapper, initialProps: { key: "AAPL" } },
    );
    await waitFor(() => expect(load).toHaveBeenCalledTimes(1));

    load.mockImplementation(async () => "second");
    rerender({ key: "MSFT" });
    await waitFor(() => expect(result.current.state).toEqual({ kind: "ready", data: "second" }));

    await act(async () => {
      resolveFirst("first");
      await Promise.resolve();
    });
    expect(result.current.state).toEqual({ kind: "ready", data: "second" });
  });

  it("maps a 404 to missing", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/ui/api/me") return json(200, me);
      throw new Error(`unexpected ${url}`);
    });
    const load = vi.fn(async () => {
      throw new ApiError(404, "not_found", "No such symbol");
    });
    const { result } = renderHook(() => usePanelData("k", load), { wrapper });
    await waitFor(() => expect(result.current.state).toEqual({ kind: "missing" }));
  });

  it("maps an empty result through isEmpty to empty", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/ui/api/me") return json(200, me);
      throw new Error(`unexpected ${url}`);
    });
    const load = vi.fn(async () => [] as string[]);
    const { result } = renderHook(
      () => usePanelData("k", load, (data: string[]) => data.length === 0),
      { wrapper },
    );
    await waitFor(() => expect(result.current.state).toEqual({ kind: "empty" }));
  });

  it("maps any other error to an error state and retry re-runs the load", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/ui/api/me") return json(200, me);
      throw new Error(`unexpected ${url}`);
    });
    let calls = 0;
    const load = vi.fn(async () => {
      calls += 1;
      if (calls === 1) throw new TypeError("network");
      return "data";
    });
    const { result } = renderHook(() => usePanelData("k", load), { wrapper });
    await waitFor(() => expect(result.current.state).toEqual({ kind: "error", message: "network" }));

    await act(async () => {
      result.current.retry();
      await Promise.resolve();
    });
    await waitFor(() => expect(result.current.state).toEqual({ kind: "ready", data: "data" }));
  });

  it("does not restart the load when isEmpty is a fresh lambda every render", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/ui/api/me") return json(200, me);
      throw new Error(`unexpected ${url}`);
    });
    const load = vi.fn(async () => "data");
    const { result, rerender } = renderHook(
      () => usePanelData("k", load, (data: string) => data === ""),
      { wrapper },
    );
    await waitFor(() => expect(result.current.state).toEqual({ kind: "ready", data: "data" }));
    rerender();
    rerender();
    expect(load).toHaveBeenCalledTimes(1);
  });
});

describe("useListKeys", () => {
  function ListKeysProbe({ count, onEnter }: { count: number; onEnter: (index: number) => void }) {
    const [selected] = useListKeys(count, onEnter);
    return <span>selected:{selected}</span>;
  }

  it("moves the selection with j/k, clamped to the row range, and Enter fires onEnter", () => {
    const onEnter = vi.fn();
    render(<ListKeysProbe count={3} onEnter={onEnter} />);
    expect(screen.getByText("selected:0")).toBeInTheDocument();

    fireEvent.keyDown(window, { key: "k" });
    expect(screen.getByText("selected:0")).toBeInTheDocument();

    fireEvent.keyDown(window, { key: "j" });
    fireEvent.keyDown(window, { key: "j" });
    expect(screen.getByText("selected:2")).toBeInTheDocument();

    fireEvent.keyDown(window, { key: "j" });
    expect(screen.getByText("selected:2")).toBeInTheDocument();

    fireEvent.keyDown(window, { key: "k" });
    expect(screen.getByText("selected:1")).toBeInTheDocument();

    fireEvent.keyDown(window, { key: "Enter" });
    expect(onEnter).toHaveBeenCalledWith(1);
  });

  it("ignores keys while an input is focused", () => {
    function WithInput() {
      const onEnter = vi.fn();
      const [selected] = useListKeys(3, onEnter);
      return (
        <>
          <input aria-label="box" />
          <span>selected:{selected}</span>
        </>
      );
    }
    render(<WithInput />);
    screen.getByLabelText("box").focus();
    fireEvent.keyDown(window, { key: "j" });
    expect(screen.getByText("selected:0")).toBeInTheDocument();
  });
});
