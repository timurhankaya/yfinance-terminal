import { cleanup, render, screen } from "@testing-library/react";
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

function registerList(code: string) {
  registerPanel({
    code,
    title: code,
    needsSymbol: false,
    layout: Layout.Single,
    parseArgs: () => ({}),
    component: ListPanel,
  });
}

function seed(id: string, code: string, symbol: string | null): PanelSeed {
  return { id, code, symbol, args: {} };
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
