import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router";
import { Choice, Controls, NumberArg, RowFilter, TextArg } from "./controls";
import { PX } from "./PX";

function LocationProbe() {
  const { pathname, search } = useLocation();
  return <span data-testid="location">{pathname + search}</span>;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("the controls", () => {
  it("picks one of a fixed set", async () => {
    const onPick = vi.fn<(value: string) => void>();
    const user = userEvent.setup();
    render(
      <Controls>
        <Choice label="Interval" value="1d" options={["1m", "5m", "1d"]} onPick={onPick} />
      </Controls>,
    );
    expect(screen.getByRole("button", { name: "1d" })).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByRole("button", { name: "5m" }));
    expect(onPick).toHaveBeenCalledWith("5m");
  });

  it("waits for Enter before asking for a different number of rows", async () => {
    const onSet = vi.fn<(value: number) => void>();
    const user = userEvent.setup();
    render(<NumberArg label="Rows" value={250} min={1} max={1000} onSet={onSet} />);
    const field = screen.getByRole("spinbutton", { name: /Rows/ });
    await user.clear(field);
    await user.type(field, "500");
    // Typing 5, then 50, then 500 would be three requests for the two the
    // reader did not mean.
    expect(onSet).not.toHaveBeenCalled();
    await user.keyboard("{Enter}");
    expect(onSet).toHaveBeenCalledExactlyOnceWith(500);
  });

  it("puts a number outside the range back rather than sending it", async () => {
    const onSet = vi.fn<(value: number) => void>();
    const user = userEvent.setup();
    render(<NumberArg label="Rows" value={250} min={1} max={1000} onSet={onSet} />);
    const field = screen.getByRole("spinbutton", { name: /Rows/ });
    await user.clear(field);
    await user.type(field, "5000{Enter}");
    expect(onSet).not.toHaveBeenCalled();
    expect(field).toHaveValue(250);
  });

  it("clears a filter by emptying its box", async () => {
    const onSet = vi.fn<(value: string) => void>();
    const user = userEvent.setup();
    render(<TextArg label="screen_key" value="day_gainers" onSet={onSet} />);
    const field = screen.getByRole("textbox", { name: "screen_key" });
    await user.clear(field);
    await user.keyboard("{Enter}");
    expect(onSet).toHaveBeenCalledWith("");
  });

  it("says how much of what is loaded a filter is showing", () => {
    render(<RowFilter value="aap" onChange={() => undefined} count={3} total={200} />);
    expect(screen.getByText("3 of 200")).toBeInTheDocument();
  });
});

describe("a control and the command line", () => {
  it("edits the same argument the command grammar does", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(
      async () =>
        new Response(JSON.stringify({ data: [], next_cursor: null }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
    );
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/ui/t/AAPL/PX?interval=1d&rows=250"]}>
        <LocationProbe />
        <PX symbol="AAPL" args={{ interval: "1d", rows: "250" }} />
      </MemoryRouter>,
    );
    await user.click(await screen.findByRole("button", { name: "5m" }));
    // `PX 5m` and clicking 5m are the same command, so the address is
    // the same either way.
    expect(screen.getByTestId("location").textContent).toBe("/ui/t/AAPL/PX?interval=5m&rows=250");
  });
});
