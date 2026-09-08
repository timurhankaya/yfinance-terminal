import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";
import { Workspace } from "./Workspace";
import type { PanelSeed, WorkspaceProps } from "./Workspace";
import { clearRegistry, registerPanel } from "../commands/registry";
import { Layout } from "../commands/types";
import type { Command } from "../commands/types";
import { useListKeys } from "../panels/common";
import { usePanelRun } from "../workspace/frame";
import { Group } from "../workspace/groups";
import { useQuote } from "../live/hooks";
import { handleFrame, resetLive, setSocketFactory } from "../live/store";
import type { SocketLike } from "../live/socket";
import { MarketHours, Op } from "../live/types";

afterEach(() => {
  cleanup();
  clearRegistry();
});

/** A panel that does the two things the frame is about: it moves a
 *  selection with j/k, and it runs a command of its own. */
function ListPanel({ symbol }: { symbol: string | null }) {
  const run = usePanelRun();
  const [selected] = useListKeys(3, () => run({ symbol, code: "LIST", args: { hit: "yes" } }));
  return (
    <div>
      <p>
        {symbol ?? "none"} row {selected}
      </p>
    </div>
  );
}

function registerList(code: string, layout = Layout.Single) {
  registerPanel({
    code,
    title: code,
    needsSymbol: layout === Layout.Headed,
    layout,
    parseArgs: () => ({}),
    component: ListPanel,
  });
}

function seed(id: string, code: string, symbol: string | null, group: Group | null = null): PanelSeed {
  return { id, code, symbol, args: {}, group };
}

/** The app always has a router around it; a panel with no frame reaches
 *  for it, so the test gives it one. */
function draw(panels: PanelSeed[], props: Partial<WorkspaceProps> = {}) {
  return render(
    <MemoryRouter>
      <Workspace panels={panels} onRun={() => undefined} {...props} />
    </MemoryRouter>,
  );
}

describe("Workspace", () => {
  it("renders a panel's body from its params", async () => {
    registerList("LIST");
    draw([seed("p1", "LIST", "AAPL")]);
    expect(await screen.findByText("AAPL row 0")).toBeInTheDocument();
  });

  it("says so when the code is not a panel", async () => {
    draw([seed("p1", "NOPE", null)]);
    expect(await screen.findByText(/Unknown function NOPE/)).toBeInTheDocument();
  });

  it("gives j to the focused panel only", async () => {
    registerList("LIST");
    const user = userEvent.setup();
    draw([seed("p1", "LIST", "AAPL"), seed("p2", "LIST", "MSFT")]);
    await screen.findByText("AAPL row 0");
    // dockview activates the panel it opened last, so MSFT has the
    // keyboard: before the frame, this `j` moved both lists.
    await user.keyboard("j");
    expect(screen.getByText("MSFT row 1")).toBeInTheDocument();
    expect(screen.getByText("AAPL row 0")).toBeInTheDocument();
  });

  it("runs a panel's command in that panel, naming it", async () => {
    registerList("LIST");
    const onRun = vi.fn<(id: string, command: Command) => void>();
    const user = userEvent.setup();
    draw([seed("p1", "LIST", "AAPL"), seed("p2", "LIST", "MSFT")], { onRun });
    await screen.findByText("MSFT row 0");
    await user.keyboard("{Enter}");
    expect(onRun).toHaveBeenCalledWith("p2", {
      symbol: "MSFT",
      code: "LIST",
      args: { hit: "yes" },
    });
  });

  it("reports which panel the keyboard is on", async () => {
    registerList("LIST");
    const onActive = vi.fn<(id: string | null) => void>();
    draw([seed("p1", "LIST", "AAPL"), seed("p2", "LIST", "MSFT")], { onActive });
    await screen.findByText("MSFT row 0");
    expect(onActive).toHaveBeenCalledWith("p2");
  });

  it("shows each panel the symbol its letter is pointed at", async () => {
    registerList("LIST");
    draw([seed("p1", "LIST", null, Group.A), seed("p2", "LIST", null, Group.B)], {
      groups: { [Group.A]: "AAPL", [Group.B]: "MSFT" },
    });
    expect(await screen.findByText("AAPL row 0")).toBeInTheDocument();
    expect(screen.getByText("MSFT row 0")).toBeInTheDocument();
  });

  it("moves every panel wearing a letter when the letter moves", async () => {
    registerList("LIST");
    const { rerender } = draw([seed("p1", "LIST", null, Group.A), seed("p2", "LIST", null, Group.A)], {
      groups: { [Group.A]: "AAPL" },
    });
    expect(await screen.findAllByText("AAPL row 0")).toHaveLength(2);
    rerender(
      <MemoryRouter>
        <Workspace
          panels={[seed("p1", "LIST", null, Group.A), seed("p2", "LIST", null, Group.A)]}
          onRun={() => undefined}
          groups={{ [Group.A]: "NVDA" }}
        />
      </MemoryRouter>,
    );
    expect(await screen.findAllByText("NVDA row 0")).toHaveLength(2);
  });

  it("gives a headed panel its own strip, so two symbols can be right at once", async () => {
    registerList("HEADED", Layout.Headed);
    const { container } = draw([
      seed("p1", "HEADED", null, Group.A),
      seed("p2", "HEADED", null, Group.B),
    ], { groups: { [Group.A]: "AAPL", [Group.B]: "MSFT" } });
    await screen.findByText("AAPL row 0");
    // One band per panel: a single one above the dock could only have
    // named one of the two symbols on screen.
    expect(container.querySelectorAll(".strip")).toHaveLength(2);
  });

  it("follows a panel's content when its params change", async () => {
    registerList("LIST");
    const { rerender } = draw([seed("p1", "LIST", "AAPL")]);
    await screen.findByText("AAPL row 0");
    rerender(
      <MemoryRouter>
        <Workspace panels={[seed("p1", "LIST", "MSFT")]} onRun={() => undefined} />
      </MemoryRouter>,
    );
    expect(await screen.findByText("MSFT row 0")).toBeInTheDocument();
    expect(screen.queryByText("AAPL row 0")).not.toBeInTheDocument();
  });
});


describe("a saved layout", () => {
  it("is restored whole rather than seeded from the panel list", async () => {
    // Sizes, splits and the active panel are exactly what an address
    // could not carry, so a stored page is handed to dockview intact.
    registerList("LIST");
    const captured: unknown[] = [];
    const { unmount } = draw([seed("list-1", "LIST", "AAPL"), seed("list-2", "LIST", "MSFT")], {
      onLayout: (dock) => captured.push(dock),
    });
    await screen.findByText("AAPL row 0");
    const dock = captured[captured.length - 1] as Parameters<
      NonNullable<WorkspaceProps["onLayout"]>
    >[0];
    expect(Object.keys(dock.panels)).toEqual(expect.arrayContaining(["list-1", "list-2"]));
    unmount();

    draw([seed("list-1", "LIST", "AAPL"), seed("list-2", "LIST", "MSFT")], { initial: dock });
    expect(await screen.findByText("AAPL row 0")).toBeInTheDocument();
  });

  it("reports a layout it cannot load and draws the panels instead", async () => {
    // The second granularity (Karar 9): only dockview can say whether a
    // stored document loads, so a page-level failure is caught here and
    // the page it came from is dropped by the caller.
    registerList("LIST");
    const failed = vi.fn();
    draw([seed("list-1", "LIST", "AAPL")], {
      initial: { grid: "not a grid" } as unknown as Parameters<
        NonNullable<WorkspaceProps["onLayout"]>
      >[0],
      onLayoutError: failed,
    });
    expect(await screen.findByText("AAPL row 0")).toBeInTheDocument();
    expect(failed).toHaveBeenCalled();
  });

  it("says nothing about layout on a page that is its own address", async () => {
    // No `onLayout` means nothing is written: the address already says
    // what the page is.
    registerList("LIST");
    draw([seed("list-1", "LIST", "AAPL")]);
    expect(await screen.findByText("AAPL row 0")).toBeInTheDocument();
  });
});


describe("a tick on a page of panels", () => {
  class DeadSocket implements SocketLike {
    onopen: ((event: unknown) => void) | null = null;
    onclose: ((event: unknown) => void) | null = null;
    onerror: ((event: unknown) => void) | null = null;
    onmessage: ((event: { data: unknown }) => void) | null = null;
    send(): void {}
    close(): void {}
  }

  const renders = new Map<string, number>();

  function Counted({ symbol }: { symbol: string | null }) {
    const quote = useQuote(symbol);
    const key = symbol ?? "none";
    renders.set(key, (renders.get(key) ?? 0) + 1);
    return (
      <p>
        {key} {quote?.p ?? "—"}
      </p>
    );
  }

  it("redraws the panels about that symbol and no others", async () => {
    // The measurement's question (spec, "Ölçümler"): with four panels
    // open, how many does one symbol's tick cost. The answer is a
    // property rather than a timing -- the store keys quotes by symbol
    // and each panel selects only its own -- so it is asserted here.
    renders.clear();
    let frames: Array<() => void> = [];
    vi.stubGlobal("requestAnimationFrame", (callback: () => void) => {
      frames.push(callback);
      return frames.length;
    });
    vi.stubGlobal("cancelAnimationFrame", () => {});
    setSocketFactory(() => new DeadSocket());
    resetLive();
    registerPanel({
      code: "Q",
      title: "Q",
      needsSymbol: true,
      layout: Layout.Single,
      parseArgs: () => ({}),
      component: Counted,
    });

    draw([
      seed("q-1", "Q", "AAPL"),
      seed("q-2", "Q", "AAPL"),
      seed("q-3", "Q", "MSFT"),
      seed("q-4", "Q", "MSFT"),
    ]);
    await screen.findAllByText(/AAPL/);
    const before = new Map(renders);

    act(() => {
      handleFrame({
        op: Op.Tick,
        d: { s: "AAPL", t: 1_000, p: "42", mh: MarketHours.Regular },
      });
      const queued = frames;
      frames = [];
      for (const callback of queued) callback();
    });

    expect(renders.get("AAPL")).toBe((before.get("AAPL") ?? 0) + 2);
    expect(renders.get("MSFT")).toBe(before.get("MSFT"));

    resetLive();
    setSocketFactory(null);
    vi.unstubAllGlobals();
  });
});
