import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { clearRegistry, registerPanel } from "../commands/registry";
import { HELP } from "./HELP";

function Dummy() {
  return null;
}

afterEach(() => {
  cleanup();
  clearRegistry();
});

describe("HELP", () => {
  it("lists every registered panel with its code, title, and symbol requirement", () => {
    clearRegistry();
    registerPanel({
      code: "DES",
      title: "Description",
      needsSymbol: true,
      layout: "headed",
      parseArgs: () => ({}),
      component: Dummy,
    });
    registerPanel({
      code: "HELP",
      title: "Help",
      needsSymbol: false,
      layout: "single",
      parseArgs: () => ({}),
      component: Dummy,
    });

    render(<HELP />);

    const desRow = screen.getByText(/^DES/);
    expect(desRow).toHaveTextContent("DES — Description (needs a symbol)");
    const helpRow = screen.getByText(/^HELP/);
    expect(helpRow).toHaveTextContent("HELP — Help (no symbol)");
  });
});
