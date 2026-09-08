// Bars, Bullet and Scatter: the accessibility pattern, the colour rule,
// and the few edges each one has.
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { Bars } from "./Bars";
import { Bullet } from "./Bullet";
import { Scatter } from "./Scatter";
import { FALLBACK, groupColor, Group } from "./colors";

afterEach(cleanup);

const money = (value: number) => value.toFixed(0);

describe("Bars", () => {
  const draw = () =>
    render(
      <Bars
        label="revenue and net income by quarter"
        categories={["Q1", "Q2", "Q3"]}
        series={[
          { key: "revenue", label: "Revenue", values: [100, 120, 90] },
          { key: "income", label: "Net income", values: [10, -5, null] },
        ]}
        line={{ label: "Net margin", values: [10, -4, null] }}
        format={money}
      />,
    );

  it("is one image with a summary", () => {
    draw();
    expect(
      screen.getByRole("img", { name: "revenue and net income by quarter" }),
    ).toBeInTheDocument();
  });

  it("takes its series colours from the group palette, in order", () => {
    const { container } = draw();
    const fills = [...container.querySelectorAll("rect")].map((r) => r.getAttribute("fill"));
    expect(fills).toContain(groupColor(Group.A));
    expect(fills).toContain(groupColor(Group.B));
  });

  it("skips a category the archive has no value for", () => {
    // Two series over three quarters is six bars at most; the null is
    // absent rather than drawn as a zero, which would read as a quarter
    // with no revenue.
    const { container } = draw();
    expect(container.querySelectorAll("svg.viz > rect")).toHaveLength(5);
  });

  it("puts zero in the domain, so a bar's length is its value", () => {
    const { container } = render(
      <Bars
        label="all above zero"
        categories={["a", "b"]}
        series={[{ key: "s", label: "S", values: [100, 110] }]}
        format={money}
      />,
    );
    const heights = [...container.querySelectorAll("rect")].map((r) =>
      Number(r.getAttribute("height")),
    );
    // 100 and 110 on a zero baseline differ by a tenth, not by everything.
    const [first = 0, second = 0] = heights;
    expect(second / first).toBeGreaterThan(1.05);
    expect(second / first).toBeLessThan(1.15);
  });

  it("writes no inline style", () => {
    const { container } = draw();
    for (const node of container.querySelectorAll("*")) {
      expect(node.getAttribute("style")).toBeNull();
    }
  });

  it("draws nothing without categories", () => {
    const { container } = render(
      <Bars label="empty" categories={[]} series={[]} format={money} />,
    );
    expect(container.querySelector("svg")).toBeNull();
  });
});

describe("Bullet", () => {
  it("is one image with a summary", () => {
    render(
      <Bullet low={100} high={200} mean={150} actual={140} label="price target" format={money} />,
    );
    expect(screen.getByRole("img", { name: "price target" })).toBeInTheDocument();
  });

  it("clips a marker that falls outside the range, and says so", () => {
    // A price above every target is exactly the case worth seeing; drawn
    // off the track it would be invisible.
    const { container } = render(
      <Bullet low={100} high={200} mean={150} actual={260} label="above" format={money} />,
    );
    const marker = container.querySelector("circle");
    expect(marker?.getAttribute("data-clipped")).toBe("above");
    expect(Number(marker?.getAttribute("cx"))).toBeLessThanOrEqual(640);
    expect(screen.getByText(/is above the range/)).toBeInTheDocument();
  });

  it("leaves an in-range marker unflagged", () => {
    const { container } = render(
      <Bullet low={100} high={200} mean={150} actual={140} label="inside" format={money} />,
    );
    expect(container.querySelector("circle")?.getAttribute("data-clipped")).toBeNull();
  });

  it("draws no marker when the actual value is unknown", () => {
    const { container } = render(
      <Bullet low={100} high={200} mean={150} actual={null} label="no price" format={money} />,
    );
    expect(container.querySelector("circle")).toBeNull();
  });

  it("draws nothing for an inverted range", () => {
    const { container } = render(
      <Bullet low={200} high={100} mean={150} actual={140} label="bad" format={money} />,
    );
    expect(container.querySelector("svg")).toBeNull();
  });
});

describe("Scatter", () => {
  const points = [
    { key: "a", label: "AAPL", x: 1, y: 10 },
    { key: "b", label: "MSFT", x: 2, y: 20 },
    { key: "c", label: "NVDA", x: 3, y: 5, colour: FALLBACK.group.a },
  ];

  it("is one image with a summary and a dot per thing", () => {
    const { container } = render(
      <Scatter points={points} label="growth against multiple" xLabel="growth" yLabel="P/E" format={money} />,
    );
    expect(screen.getByRole("img", { name: "growth against multiple" })).toBeInTheDocument();
    expect(container.querySelectorAll("circle")).toHaveLength(3);
  });

  it("names each dot, so a cloud is still readable one point at a time", () => {
    // An SVG `<title>` inside the shape, which is what a hover reads and
    // what an assistive technology announces for that node.
    const { container } = render(
      <Scatter points={points} label="cloud" xLabel="growth" yLabel="P/E" format={money} />,
    );
    const names = [...container.querySelectorAll("circle > title")].map((t) => t.textContent);
    expect(names).toEqual(["AAPL", "MSFT", "NVDA"]);
  });

  it("draws the reference line in data coordinates and names it", () => {
    const { container } = render(
      <Scatter
        points={points}
        label="cloud"
        xLabel="growth"
        yLabel="P/E"
        reference={{ x1: 1, y1: 5, x2: 3, y2: 20, label: "fair value" }}
        format={money}
      />,
    );
    const line = container.querySelector("[data-reference]");
    expect(line?.getAttribute("data-reference")).toBe("fair value");
    expect(screen.getByText(/dashed: fair value/)).toBeInTheDocument();
  });

  it("writes no inline style", () => {
    const { container } = render(
      <Scatter points={points} label="cloud" xLabel="x" yLabel="y" format={money} />,
    );
    for (const node of container.querySelectorAll("*")) {
      expect(node.getAttribute("style")).toBeNull();
    }
  });

  it("draws nothing when no point has two finite coordinates", () => {
    const { container } = render(
      <Scatter
        points={[{ key: "a", label: "A", x: Number.NaN, y: 1 }]}
        label="cloud"
        xLabel="x"
        yLabel="y"
        format={money}
      />,
    );
    expect(container.querySelector("svg")).toBeNull();
  });
});
