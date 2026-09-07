import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { MemoryRouter, useLocation } from "react-router";
import { clearRegistry, registerPanel } from "../commands/registry";
import { HELP } from "./HELP";

function Dummy() {
  return null;
}

function LocationProbe() {
  const { pathname } = useLocation();
  return <span data-testid="location">{pathname}</span>;
}

afterEach(() => {
  cleanup();
  clearRegistry();
});

function registerTwo() {
  clearRegistry();
  registerPanel({ code: "DES", title: "Description", needsSymbol: true, layout: "headed", parseArgs: () => ({}), component: Dummy });
  registerPanel({ code: "HELP", title: "Help", needsSymbol: false, layout: "single", parseArgs: () => ({}), component: Dummy });
}

function renderHelp(symbol: string | null) {
  return render(
    <MemoryRouter initialEntries={["/ui/t/-/HELP"]}>
      <LocationProbe />
      <HELP symbol={symbol} args={{}} />
    </MemoryRouter>,
  );
}

describe("HELP", () => {
  it("lists every registered panel with its code, title, and symbol requirement", () => {
    registerTwo();
    renderHelp(null);
    expect(screen.getByRole("button", { name: "DES" }).closest("li")).toHaveTextContent("DES — Description (needs a symbol)");
    expect(screen.getByRole("button", { name: "HELP" }).closest("li")).toHaveTextContent("HELP — Help (no symbol)");
  });

  it("runs a function on the context symbol when clicked", async () => {
    registerTwo();
    renderHelp("AAPL");
    await userEvent.click(screen.getByRole("button", { name: "DES" }));
    expect(screen.getByTestId("location").textContent).toBe("/ui/t/AAPL/DES");
  });

  it("disables functions that need a symbol when there is none", () => {
    registerTwo();
    renderHelp(null);
    expect(screen.getByRole("button", { name: "DES" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "HELP" })).toBeEnabled();
  });
});
