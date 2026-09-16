// The band is navigation, so what it is tested on is reach: which codes
// it offers, which panel a click lands in, and the two panels that must
// not be given one.
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SymbolBand } from "./SymbolBand";
import { registerPanel } from "../commands/registry";
import { Layout } from "../commands/types";
import type { Command } from "../commands/types";
import { FrameProvider } from "../workspace/frame";

function panel(code: string, needsSymbol: boolean, layout = Layout.Single) {
  registerPanel({
    code,
    title: `${code} panel`,
    needsSymbol,
    layout,
    parseArgs: () => ({}),
    component: () => null,
  });
}

panel("DES", true, Layout.Headed);
panel("ANR", true);
panel("HEAT", false);

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function draw(code: string, live = false) {
  const run = vi.fn<(command: Command) => void>();
  // A Router as well as a frame: `usePanelRun` reaches for `useGo` on
  // every render, frame or no frame, because a hook cannot be called
  // conditionally.
  render(
    <MemoryRouter>
      <FrameProvider value={{ run, focused: true }}>
        <SymbolBand symbol="AAPL" code={code} live={live} />
      </FrameProvider>
    </MemoryRouter>,
  );
  return { run, band: screen.getByRole("navigation", { name: "AAPL functions" }) };
}

describe("SymbolBand", () => {
  it("offers the symbol functions and no market page", () => {
    const { band } = draw("DES");
    expect(within(band).getByRole("button", { name: "DES" })).toBeInTheDocument();
    expect(within(band).getByRole("button", { name: "ANR" })).toBeInTheDocument();
    expect(within(band).queryByRole("button", { name: "HEAT" })).not.toBeInTheDocument();
  });

  it("marks the code this panel is showing, and only that one", () => {
    const { band } = draw("ANR");
    expect(within(band).getByRole("button", { name: "ANR" })).toHaveAttribute("aria-current", "page");
    expect(within(band).getByRole("button", { name: "DES" })).not.toHaveAttribute("aria-current");
  });

  it("runs the function in THIS panel, not in the address", () => {
    // The frame is the whole reason the band can live inside a panel: on
    // a page of four, clicking ANR must replace one of them rather than
    // navigate the page away.
    const { run, band } = draw("DES");
    fireEvent.click(within(band).getByRole("button", { name: "ANR" }));
    expect(run).toHaveBeenCalledWith({ symbol: "AAPL", code: "ANR", args: {} });
  });

  it("carries a live price only where the layout says there is one", () => {
    const { band } = draw("ANR", false);
    expect(band.parentElement).toHaveClass("band");
    expect(screen.queryByText("no live price")).not.toBeInTheDocument();
    cleanup();
    draw("DES", true);
    // `Strip` says what it does not know rather than showing a blank.
    expect(screen.getByText("no live price")).toBeInTheDocument();
  });
});
