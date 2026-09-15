import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { TooltipLayer } from "./TooltipLayer";

afterEach(() => { cleanup(); vi.useRealTimers(); });

it("adds and removes help without replacing an existing accessible description", () => {
  render(<><TooltipLayer /><button data-tooltip="Financial statements" aria-describedby="existing">FA</button><p id="existing">Saved panel</p></>);
  const button = screen.getByRole("button");
  fireEvent.focusIn(button);
  const tooltip = screen.getByRole("tooltip");
  expect(tooltip).toHaveTextContent("Financial statements");
  expect(button.getAttribute("aria-describedby")).toBe(`existing ${tooltip.id}`);
  fireEvent.keyDown(button, { key: "Escape" });
  expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  expect(button).toHaveAttribute("aria-describedby", "existing");
});

it("keeps hover help open while the pointer moves onto the tooltip", () => {
  vi.useFakeTimers();
  render(<><TooltipLayer /><button data-tooltip="Compare periods">FA</button></>);
  const button = screen.getByRole("button");
  fireEvent.pointerOver(button);
  vi.advanceTimersByTime(250);
  const tooltip = screen.getByRole("tooltip");
  fireEvent.pointerOut(button, { relatedTarget: tooltip });
  fireEvent.pointerOver(tooltip, { relatedTarget: button });
  vi.advanceTimersByTime(500);
  expect(tooltip).toBeInTheDocument();
  fireEvent.pointerOut(tooltip, { relatedTarget: document.body });
  vi.advanceTimersByTime(120);
  expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
});

it("does not open a delayed tooltip after the pointer leaves", () => {
  vi.useFakeTimers();
  render(<><TooltipLayer /><button data-tooltip="Compare periods">FA</button></>);
  const button = screen.getByRole("button");
  fireEvent.pointerOver(button);
  fireEvent.pointerOut(button, { relatedTarget: document.body });
  vi.advanceTimersByTime(500);
  expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
});

it("removes orphaned help when a panel is closed", async () => {
  const view = render(<><TooltipLayer /><button data-tooltip="Compare periods">FA</button></>);
  fireEvent.focusIn(screen.getByRole("button"));
  expect(screen.getByRole("tooltip")).toBeInTheDocument();
  view.rerender(<TooltipLayer />);
  await waitFor(() => expect(screen.queryByRole("tooltip")).not.toBeInTheDocument());
});

it("dismisses help after activating a tab so it cannot cover the next control", () => {
  render(<><TooltipLayer /><button data-tooltip="Institutional holdings">HDS</button></>);
  const button = screen.getByRole("button");
  fireEvent.focusIn(button);
  expect(screen.getByRole("tooltip")).toBeInTheDocument();
  fireEvent.click(button);
  expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
});
