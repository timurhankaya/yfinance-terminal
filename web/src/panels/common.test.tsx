import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { LoadState, useKeptData, useListKeys, usePagedRows, usePanelData, type Loaded } from "./common";

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
    await waitFor(() => expect(result.current.state).toEqual({ kind: LoadState.Ready, data: "data" }));
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
    await waitFor(() => expect(result.current.state).toEqual({ kind: LoadState.Ready, data: "first" }));

    load.mockImplementation(async () => "second");
    rerender({ key: "MSFT" });
    await waitFor(() => expect(result.current.state).toEqual({ kind: LoadState.Ready, data: "second" }));
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
    await waitFor(() => expect(result.current.state).toEqual({ kind: LoadState.Ready, data: "second" }));

    await act(async () => {
      resolveFirst("first");
      await Promise.resolve();
    });
    expect(result.current.state).toEqual({ kind: LoadState.Ready, data: "second" });
  });

  it("maps a 404 to missing", async () => {
    noNetwork();
    const load = vi.fn(async () => {
      throw new ApiError(404, "not_found", "No such symbol");
    });
    const { result } = renderHook(() => usePanelData("k", load));
    await waitFor(() => expect(result.current.state).toEqual({ kind: LoadState.Missing }));
  });

  it("maps a 401 to a plain error, not a state of its own", async () => {
    noNetwork();
    const load = vi.fn(async () => {
      throw new ApiError(401, "unauthenticated", "Unauthenticated");
    });
    const { result } = renderHook(() => usePanelData("k", load));
    await waitFor(() => expect(result.current.state).toEqual({ kind: LoadState.Error, message: "Unauthenticated" }));
  });

  it("maps an empty result through isEmpty to empty", async () => {
    noNetwork();
    const load = vi.fn(async () => [] as string[]);
    const { result } = renderHook(() => usePanelData("k", load, (data: string[]) => data.length === 0));
    await waitFor(() => expect(result.current.state).toEqual({ kind: LoadState.Empty }));
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
    await waitFor(() => expect(result.current.state).toEqual({ kind: LoadState.Error, message: "network" }));

    await act(async () => {
      result.current.retry();
      await Promise.resolve();
    });
    await waitFor(() => expect(result.current.state).toEqual({ kind: LoadState.Ready, data: "data" }));
  });

  it("does not restart the load when isEmpty is a fresh lambda every render", async () => {
    noNetwork();
    const load = vi.fn(async () => "data");
    const { result, rerender } = renderHook(() => usePanelData("k", load, (data: string) => data === ""));
    await waitFor(() => expect(result.current.state).toEqual({ kind: LoadState.Ready, data: "data" }));
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

  it.each([
    ["button", <button key="b" type="button">Load more</button>],
    ["link", <a key="a" href="https://example.com">EDGAR</a>],
    ["tab", <button key="t" type="button" role="tab">Income</button>],
  ])("ignores keys while a %s holds focus", (_what, control) => {
    // A button keeps focus after a click, so an unguarded Enter would
    // re-click "Load more" AND open the selected row; j/k would move the
    // selection while a tab still has the keyboard.
    const onEnter = vi.fn();
    function WithControl() {
      const [selected] = useListKeys(3, onEnter);
      return (
        <>
          {control}
          <span>selected:{selected}</span>
        </>
      );
    }
    render(<WithControl />);
    const el = screen.getByText(/Load more|EDGAR|Income/);
    el.focus();
    expect(el).toHaveFocus();
    fireEvent.keyDown(window, { key: "j" });
    fireEvent.keyDown(window, { key: "Enter" });
    expect(screen.getByText("selected:0")).toBeInTheDocument();
    expect(onEnter).not.toHaveBeenCalled();
  });

  it("still answers keys when nothing interactive is focused", () => {
    const onEnter = vi.fn();
    function WithRow() {
      const [selected] = useListKeys(3, onEnter);
      return (
        <>
          <li>a row</li>
          <span>selected:{selected}</span>
        </>
      );
    }
    render(<WithRow />);
    fireEvent.keyDown(window, { key: "j" });
    expect(screen.getByText("selected:1")).toBeInTheDocument();
  });
});

describe("useKeptData", () => {
  const ready = (data: string): Loaded<string> => ({ kind: LoadState.Ready, data });
  const loading: Loaded<string> = { kind: LoadState.Loading };

  it("keeps the last payload of the same query while a reload is in flight", () => {
    const { result, rerender } = renderHook(
      ({ key, state }: { key: string; state: Loaded<string> }) => useKeptData(key, state),
      { initialProps: { key: "AAPL", state: ready("first") } },
    );
    expect(result.current).toBe("first");
    // The same query, loading again: what is on screen must survive it.
    rerender({ key: "AAPL", state: loading });
    expect(result.current).toBe("first");
    rerender({ key: "AAPL", state: ready("second") });
    expect(result.current).toBe("second");
  });

  it("drops it when the query itself changes", () => {
    // Showing AAPL's bars under an MSFT heading would be a lie.
    const { result, rerender } = renderHook(
      ({ key, state }: { key: string; state: Loaded<string> }) => useKeptData(key, state),
      { initialProps: { key: "AAPL", state: ready("aapl") } },
    );
    expect(result.current).toBe("aapl");
    rerender({ key: "MSFT", state: loading });
    expect(result.current).toBeNull();
  });
});

describe("usePagedRows", () => {
  function page(rows: string[], next: string | null) {
    return { rows, next_cursor: next };
  }

  it("appends a page and takes the new cursor", async () => {
    const fetchPage = vi.fn(async (cursor: string) => page([`row-${cursor}`], null));
    const { result } = renderHook(() => usePagedRows("k", "c1", fetchPage));
    expect(result.current.cursor).toBe("c1");

    await act(async () => {
      result.current.loadMore();
    });
    await waitFor(() => expect(result.current.rows).toEqual(["row-c1"]));
    expect(result.current.cursor).toBeNull();
    expect(result.current.error).toBeNull();
  });

  it("says why a page failed instead of swallowing the rejection", async () => {
    // A button that silently does nothing is indistinguishable from a
    // list with no next page.
    const fetchPage = vi.fn(async () => {
      throw new ApiError(500, "internal", "Server error");
    });
    const { result } = renderHook(() => usePagedRows<string>("k", "c1", fetchPage));
    await act(async () => {
      result.current.loadMore();
    });
    await waitFor(() => expect(result.current.error).toBe("Server error"));
    expect(result.current.loadingMore).toBe(false);
    // The cursor survives, so the reader can try again.
    expect(result.current.cursor).toBe("c1");
  });

  it("ignores a second request while one page is in flight", async () => {
    let release!: (value: { rows: string[]; next_cursor: string | null }) => void;
    const pending = new Promise<{ rows: string[]; next_cursor: string | null }>((resolve) => {
      release = resolve;
    });
    const fetchPage = vi.fn(() => pending);
    const { result } = renderHook(() => usePagedRows("k", "c1", fetchPage));

    act(() => {
      result.current.loadMore();
    });
    expect(result.current.loadingMore).toBe(true);
    act(() => {
      result.current.loadMore();
    });
    expect(fetchPage).toHaveBeenCalledTimes(1);

    await act(async () => {
      release(page(["one"], null));
      await Promise.resolve();
    });
    await waitFor(() => expect(result.current.rows).toEqual(["one"]));
  });

  it("drops the pages already read when the query changes", async () => {
    const fetchPage = vi.fn(async () => page(["extra"], "c2"));
    const { result, rerender } = renderHook(
      ({ key }: { key: string }) => usePagedRows(key, "c1", fetchPage),
      { initialProps: { key: "a" } },
    );
    await act(async () => {
      result.current.loadMore();
    });
    await waitFor(() => expect(result.current.rows).toEqual(["extra"]));

    rerender({ key: "b" });
    expect(result.current.rows).toEqual([]);
    expect(result.current.cursor).toBe("c1");
  });
});
