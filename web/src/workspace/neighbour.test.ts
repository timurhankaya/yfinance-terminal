import { describe, expect, it } from "vitest";
import { Direction, pickNeighbour } from "./neighbour";
import type { Box } from "./neighbour";

/** A page split like a terminal usually is: a tall chart on the left,
 *  two stacked panels on the right, and a wide tape underneath. */
const PAGE: Box[] = [
  { id: "chart", left: 0, top: 0, width: 600, height: 400 },
  { id: "top-right", left: 600, top: 0, width: 400, height: 200 },
  { id: "bottom-right", left: 600, top: 200, width: 400, height: 200 },
  { id: "tape", left: 0, top: 400, width: 1000, height: 200 },
];

describe("pickNeighbour", () => {
  it("finds the panel on each side", () => {
    expect(pickNeighbour(PAGE, "chart", Direction.Right)).toBe("top-right");
    expect(pickNeighbour(PAGE, "top-right", Direction.Left)).toBe("chart");
    expect(pickNeighbour(PAGE, "top-right", Direction.Down)).toBe("bottom-right");
    expect(pickNeighbour(PAGE, "bottom-right", Direction.Up)).toBe("top-right");
  });

  it("stops at the edge of the page", () => {
    expect(pickNeighbour(PAGE, "chart", Direction.Left)).toBeNull();
    expect(pickNeighbour(PAGE, "chart", Direction.Up)).toBeNull();
    expect(pickNeighbour(PAGE, "tape", Direction.Down)).toBeNull();
  });

  it("prefers the panel whose middle is closest, not the first one made", () => {
    // From the chart, both right-hand panels are the same distance away;
    // the one level with it wins.
    const level: Box[] = [
      { id: "chart", left: 0, top: 100, width: 600, height: 200 },
      { id: "far", left: 600, top: 0, width: 400, height: 100 },
      { id: "level", left: 600, top: 150, width: 400, height: 100 },
    ];
    expect(pickNeighbour(level, "chart", Direction.Right)).toBe("level");
  });

  it("does not call a panel below 'right' just because it is not left", () => {
    expect(pickNeighbour(PAGE, "chart", Direction.Down)).toBe("tape");
    // The tape is beneath the chart, not beside it.
    expect(pickNeighbour([PAGE[0]!, PAGE[3]!], "chart", Direction.Right)).toBeNull();
  });

  it("answers null when the panel is not on the page", () => {
    expect(pickNeighbour(PAGE, "gone", Direction.Left)).toBeNull();
    expect(pickNeighbour([], "chart", Direction.Left)).toBeNull();
  });
});
