import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { useListKeys, usePanelData } from "./common";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

// Every test guards the network: usePanelData must only ever call the `load`
// it was handed, never reach for an endpoint of its own.
function noNetwork() {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    throw new Error(`unexpected ${String(input)}`);
  });
}

describe("usePanelData", () => {
  it("loads once on mount", async () => {
    noNetwork();
    const load = vi.fn(async () => "data");
    const { result, rerender } = renderHook(() => usePanelData("k", load));
    await waitFor(() => expect(result.current.state).toEqual({ kind: "ready", data: "data" }));
    rerender();
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("re-runs the load when the key changes", async () => {
    noNetwork();
    const load = vi.fn(async () => "first");
    const { result, rerender } = renderHook(
      ({ key }: { key: string }) => usePanelData(key, load),
      { initialProps: { key: "AAPL" } },
    );
    await waitFor(() => expect(result.current.state).toEqual({ kind: "ready", data: "first" }));

    load.mockImplementation(async () => "second");
    rerender({ key: "MSFT" });
    await waitFor(() => expect(result.current.state).toEqual({ kind: "ready", data: "second" }));
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("drops a stale response when the key changes before it resolves", async () => {
    noNetwork();
    let resolveFirst!: (value: string) => void;
    const first = new Promise<string>((resolve) => {
      resolveFirst = resolve;
    });
    const load = vi.fn(() => first);
    const { result, rerender } = renderHook(
      ({ key }: { key: string }) => usePanelData(key, load),
      { initialProps: { key: "AAPL" } },
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
    noNetwork();
    const load = vi.fn(async () => {
      throw new ApiError(404, "not_found", "No such symbol");
    });
    const { result } = renderHook(() => usePanelData("k", load));
    await waitFor(() => expect(result.current.state).toEqual({ kind: "missing" }));
  });

  it("maps a 401 to a plain error, not a state of its own", async () => {
    noNetwork();
    const load = vi.fn(async () => {
      throw new ApiError(401, "unauthenticated", "Unauthenticated");
    });
    const { result } = renderHook(() => usePanelData("k", load));
    await waitFor(() => expect(result.current.state).toEqual({ kind: "error", message: "Unauthenticated" }));
  });

  it("maps an empty result through isEmpty to empty", async () => {
    noNetwork();
    const load = vi.fn(async () => [] as string[]);
    const { result } = renderHook(() => usePanelData("k", load, (data: string[]) => data.length === 0));
    await waitFor(() => expect(result.current.state).toEqual({ kind: "empty" }));
  });

  it("maps any other error to an error state and retry re-runs the load", async () => {
    noNetwork();
    let calls = 0;
    const load = vi.fn(async () => {
      calls += 1;
      if (calls === 1) throw new TypeError("network");
      return "data";
    });
    const { result } = renderHook(() => usePanelData("k", load));
    await waitFor(() => expect(result.current.state).toEqual({ kind: "error", message: "network" }));

    await act(async () => {
      result.current.retry();
      await Promise.resolve();
    });
    await waitFor(() => expect(result.current.state).toEqual({ kind: "ready", data: "data" }));
  });

  it("does not restart the load when isEmpty is a fresh lambda every render", async () => {
    noNetwork();
    const load = vi.fn(async () => "data");
    const { result, rerender } = renderHook(() => usePanelData("k", load, (data: string) => data === ""));
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
