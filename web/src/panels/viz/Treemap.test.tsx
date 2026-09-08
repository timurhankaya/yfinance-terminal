import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MAX_CELLS, OTHER_KEY, Treemap, capCells, squarify } from "./Treemap";
import type { TreemapItem } from "./Treemap";

afterEach(cleanup);

const item = (key: string, value: number, percent: number | null = 0): TreemapItem => ({
  key,
  label: key,
  value,
  percent,
});

const many = (count: number): TreemapItem[] =>
  Array.from({ length: count }, (_, index) => item(`s${index}`, count - index));

describe("squarify", () => {
  it("gives every box an area proportional to its value", () => {
    const items = [item("a", 6), item("b", 3), item("c", 1)];
    const boxes = squarify(items, 100, 100);
    const total = boxes.reduce((sum, box) => sum + box.width * box.height, 0);
    expect(total).toBeCloseTo(10_000, 2);
    const byKey = new Map(boxes.map((box) => [box.key, box.width * box.height]));
    expect(byKey.get("a")).toBeCloseTo(6_000, 1);
    expect(byKey.get("b")).toBeCloseTo(3_000, 1);
    expect(byKey.get("c")).toBeCloseTo(1_000, 1);
  });

  it("keeps every box inside the rectangle", () => {
    for (const box of squarify(many(400), 960, 540)) {
      expect(box.x).toBeGreaterThanOrEqual(-1e-6);
      expect(box.y).toBeGreaterThanOrEqual(-1e-6);
      expect(box.x + box.width).toBeLessThanOrEqual(960 + 1e-6);
      expect(box.y + box.height).toBeLessThanOrEqual(540 + 1e-6);
    }
  });

  it("draws the boxes largest first", () => {
    const values = squarify([item("a", 1), item("b", 9), item("c", 5)], 100, 100).map(
      (box) => box.value,
    );
    expect(values).toEqual([9, 5, 1]);
  });

  it("drops what has no area rather than drawing a zero-width box", () => {
    expect(squarify([item("a", 5), item("b", 0), item("c", -3)], 100, 100)).toHaveLength(1);
  });

  it("is empty when there is nothing to draw", () => {
    expect(squarify([], 100, 100)).toEqual([]);
    expect(squarify([item("a", 5)], 0, 100)).toEqual([]);
  });
});

describe("capCells", () => {
  it("leaves a list at the limit alone", () => {
    expect(capCells(many(MAX_CELLS))).toHaveLength(MAX_CELLS);
    expect(capCells(many(MAX_CELLS)).some((cell) => cell.key === OTHER_KEY)).toBe(false);
  });

  it("collects the tail into one box that says how many it holds", () => {
    const capped = capCells(many(MAX_CELLS + 50));
    expect(capped).toHaveLength(MAX_CELLS);
    const other = capped[capped.length - 1];
    expect(other?.key).toBe(OTHER_KEY);
    expect(other?.label).toBe("Other (51)");
    // Nothing is lost: the tail's area is the tail's total.
    const tail = many(MAX_CELLS + 50).slice(MAX_CELLS - 1);
    expect(other?.value).toBe(tail.reduce((sum, cell) => sum + cell.value, 0));
    // And it draws neutral: there is no single move for 51 things.
    expect(other?.percent).toBeNull();
  });
});

describe("Treemap", () => {
  const draw = (items: TreemapItem[], onOpen = vi.fn()) =>
    render(
      <Treemap
        items={items}
        label="sectors"
        span={5}
        onOpen={onOpen}
        format={(cell) => `${cell.value}`}
      />,
    );

  it("is a group of buttons, not one image", () => {
    // Decision 8: `role="img"` would drop every box out of the
    // accessibility tree, and the boxes are the content.
    draw([item("Tech", 10, 2), item("Energy", 5, -3)]);
    const group = screen.getByRole("group", { name: "sectors" });
    expect(group.tagName.toLowerCase()).toBe("svg");
    expect(screen.getAllByRole("button")).toHaveLength(2);
  });

  it("opens a box on Enter as well as on a click", () => {
    const onOpen = vi.fn();
    draw([item("Tech", 10, 2)], onOpen);
    const box = screen.getByRole("button", { name: /Tech/ });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onOpen).toHaveBeenCalledWith("Tech");
    fireEvent.click(box);
    expect(onOpen).toHaveBeenCalledTimes(2);
  });

  it("writes no text in a box too small to hold it", () => {
    // 200 boxes over a 960x540 viewBox leaves most of them under the
    // threshold; half a letter is noise.
    const { container } = draw(many(200));
    const labels = container.querySelectorAll("text.viz-cell-label");
    expect(labels.length).toBeLessThan(200);
    expect(labels.length).toBeGreaterThan(0);
  });

  it("always draws the scale, since colour alone carries no value", () => {
    // Decision 7.
    const { container } = draw([item("Tech", 10, 2)]);
    expect(container.querySelector('[data-legend="heat"]')).not.toBeNull();
    expect(screen.getByText("+5%")).toBeInTheDocument();
    expect(screen.getByText("−5%")).toBeInTheDocument();
  });

  it("gives colour as an attribute, never as inline style", () => {
    const { container } = draw([item("Tech", 10, 2), item("Energy", 5, -3)]);
    for (const node of container.querySelectorAll("*")) {
      expect(node.getAttribute("style")).toBeNull();
    }
    expect(container.querySelector("rect")?.getAttribute("fill")).toMatch(/^#[0-9a-f]{6}$/);
  });

  it("draws nothing when there is nothing to draw", () => {
    const { container } = draw([]);
    expect(container.querySelector("svg")).toBeNull();
  });
});
