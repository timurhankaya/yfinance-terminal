import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { Sparkline } from "./Sparkline";
import { FALLBACK } from "./colors";

afterEach(cleanup);

describe("Sparkline", () => {
  it("is one image with a summary, not a tree of unreadable shapes", () => {
    // Decision 8: a chart nobody can click is `role="img"` with a label
    // that says what it shows -- the same pattern `Chart.tsx:176` set.
    render(<Sparkline values={[1, 2, 3]} label="AAPL, 3 sessions, up" />);
    const svg = screen.getByRole("img", { name: "AAPL, 3 sessions, up" });
    expect(svg.tagName.toLowerCase()).toBe("svg");
  });

  it("draws a point per session", () => {
    const { container } = render(<Sparkline values={[1, 2, 3, 4]} label="four" />);
    const d = container.querySelector("path")?.getAttribute("d") ?? "";
    expect(d.split("L")).toHaveLength(4);
  });

  it("colours a rise up and a fall down, by first against last", () => {
    const { container: rising } = render(<Sparkline values={[1, 5, 9]} label="up" />);
    expect(rising.querySelector("path")?.getAttribute("stroke")).toBe(FALLBACK.up);
    cleanup();
    const { container: falling } = render(<Sparkline values={[9, 5, 1]} label="down" />);
    expect(falling.querySelector("path")?.getAttribute("stroke")).toBe(FALLBACK.down);
  });

  it("gives colour as an attribute, never as inline style", () => {
    // Decision 6: `style-src 'self'` blocks a style attribute outright,
    // and `web/src` has none.
    const { container } = render(<Sparkline values={[1, 2]} label="two" area />);
    for (const node of container.querySelectorAll("*")) {
      expect(node.getAttribute("style")).toBeNull();
    }
    expect(container.querySelectorAll("path")).toHaveLength(2); // area + line
  });

  it("draws nothing at all below two points", () => {
    // One close is not a shape. The cell says "no data"; a single dot
    // would read as a flat month.
    const { container } = render(<Sparkline values={[7]} label="one" />);
    expect(container.querySelector("svg")).toBeNull();
  });

  it("is memoised, so a ticking row does not redraw it", () => {
    // `WLA` holds 200 of these and every row re-renders on its own tick.
    // The property is structural rather than timed: `memo` is the whole
    // mechanism, and asserting the wrapper is what asserts the mechanism
    // (the render-count half is `live/hooks.test.tsx`).
    const wrapper = Sparkline as unknown as { $$typeof: symbol };
    expect(wrapper.$$typeof).toBe(Symbol.for("react.memo"));
  });
});
