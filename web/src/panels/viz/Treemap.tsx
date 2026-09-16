// A treemap: area is size, colour is the move. Squarified rather than
// sliced: a cell has to be readable AND comparable, and long thin slivers
// are neither. Every box is focusable and answers Enter, so this is
// `role="group"` and NOT `role="img"`, which would drop the boxes from
// the accessibility tree.
import { useEffect, useRef, useState, type ReactElement } from "react";
import { divergingHeat } from "./colors";
import { useVizTheme } from "./useVizTheme";

export interface TreemapItem {
  key: string;
  label: string;
  /** The area. Non-positive values are dropped: there is no such box. */
  value: number;
  /** The move, as a percentage. Null draws neutral. */
  percent: number | null;
}

export interface TreemapBox extends TreemapItem {
  x: number;
  y: number;
  width: number;
  height: number;
}

//: Past this the tail is one box (`treemap.bench.ts` is the gate).
export const MAX_CELLS = 400;

//: A label needs room to be a label rather than a smear.
const LABEL_WIDTH = 46;
const LABEL_HEIGHT = 18;

const WIDTH = 960;
const HEIGHT = 540;

/** The key the overflow box carries. One box, named, so the panel can
 *  say how many things are inside it. */
export const OTHER_KEY = "__other";

/** At most `max` boxes: the largest, and one holding everything else.
 *
 *  Dropping the tail silently would make a treemap of the 500 biggest
 *  companies look like a treemap of the market. */
export function capCells(items: readonly TreemapItem[], max: number = MAX_CELLS): TreemapItem[] {
  const usable = items.filter((item) => Number.isFinite(item.value) && item.value > 0);
  const sorted = [...usable].sort((a, b) => b.value - a.value);
  if (sorted.length <= max) return sorted;
  const kept = sorted.slice(0, max - 1);
  const rest = sorted.slice(max - 1);
  const value = rest.reduce((total, item) => total + item.value, 0);
  return [
    ...kept,
    {
      key: OTHER_KEY,
      // The count is in the name because the box's own area does not say
      // how many things it holds.
      label: `Other (${rest.length})`,
      value,
      // No single move to show: an average across the tail would be a
      // number nobody asked for.
      percent: null,
    },
  ];
}

interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

function worst(areas: readonly number[], length: number): number {
  if (areas.length === 0 || length === 0) return Number.POSITIVE_INFINITY;
  let sum = 0;
  let min = Number.POSITIVE_INFINITY;
  let max = 0;
  for (const area of areas) {
    sum += area;
    if (area < min) min = area;
    if (area > max) max = area;
  }
  if (sum === 0 || min === 0) return Number.POSITIVE_INFINITY;
  return Math.max((length * length * max) / (sum * sum), (sum * sum) / (length * length * min));
}

function layRow(
  row: readonly TreemapItem[],
  areas: readonly number[],
  rect: Rect,
  out: TreemapBox[],
): Rect {
  const total = areas.reduce((sum, area) => sum + area, 0);
  const horizontal = rect.width <= rect.height;
  const length = horizontal ? rect.width : rect.height;
  const thickness = length === 0 ? 0 : total / length;
  let offset = 0;
  row.forEach((item, index) => {
    const area = areas[index] ?? 0;
    const extent = thickness === 0 ? 0 : area / thickness;
    out.push({
      ...item,
      x: horizontal ? rect.x + offset : rect.x,
      y: horizontal ? rect.y : rect.y + offset,
      width: horizontal ? extent : thickness,
      height: horizontal ? thickness : extent,
    });
    offset += extent;
  });
  return horizontal
    ? { x: rect.x, y: rect.y + thickness, width: rect.width, height: rect.height - thickness }
    : { x: rect.x + thickness, y: rect.y, width: rect.width - thickness, height: rect.height };
}

/** Squarified layout (Bruls, Huizing and van Wijk): boxes whose area is
 *  proportional to `value`, laid along the shorter side of what is left,
 *  which keeps the aspect ratios near one. */
export function squarify(
  items: readonly TreemapItem[],
  width: number,
  height: number,
): TreemapBox[] {
  const usable = [...items]
    .filter((item) => Number.isFinite(item.value) && item.value > 0)
    .sort((a, b) => b.value - a.value);
  const total = usable.reduce((sum, item) => sum + item.value, 0);
  if (total <= 0 || width <= 0 || height <= 0) return [];

  const out: TreemapBox[] = [];
  let rect: Rect = { x: 0, y: 0, width, height };
  let remaining = usable;
  let scale = (width * height) / total;

  while (remaining.length > 0) {
    const length = Math.min(rect.width, rect.height);
    if (length <= 0) break;
    const row: TreemapItem[] = [];
    const areas: number[] = [];
    while (remaining.length > 0) {
      const next = remaining[0];
      if (next === undefined) break;
      const area = next.value * scale;
      if (row.length > 0 && worst([...areas, area], length) > worst(areas, length)) break;
      row.push(next);
      areas.push(area);
      remaining = remaining.slice(1);
    }
    if (row.length === 0) break;
    rect = layRow(row, areas, rect, out);
    // The scale is recomputed from what is left against what is left of
    // the rectangle: drift over four hundred rows would otherwise show
    // up as a last box that does not reach the edge.
    const leftover = remaining.reduce((sum, item) => sum + item.value, 0);
    if (leftover <= 0 || rect.width <= 0 || rect.height <= 0) break;
    scale = (rect.width * rect.height) / leftover;
  }
  return out;
}

export interface TreemapProps {
  items: readonly TreemapItem[];
  label: string;
  /** The change, in percent, that reaches full colour. */
  span: number;
  /** Enter or a click on a box. Every box is focusable because of it. */
  onOpen: (key: string) => void;
  format: (item: TreemapItem) => string;
}

export function Treemap(props: TreemapProps): ReactElement | null {
  const { items, label, span, onOpen, format } = props;
  const theme = useVizTheme();
  const host = useRef<HTMLElement>(null);
  const [width, setWidth] = useState(WIDTH);
  const height = width * HEIGHT / WIDTH;
  // Use actual pixels for the viewBox so labels stay 11px in narrow tiles.
  const hasItems = items.some((item) => Number.isFinite(item.value) && item.value > 0);
  useEffect(() => {
    const element = host.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry && entry.contentRect.width > 0) setWidth(entry.contentRect.width);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [hasItems]);
  const boxes = squarify(capCells(items), width, height);
  if (boxes.length === 0) return null;

  return (
    <figure className="viz-figure" ref={host}>
      <svg
        className="viz viz-treemap"
        viewBox={`0 0 ${width} ${height}`}
        role="group"
        aria-label={label}
      >
        {boxes.map((box) => {
          const roomy = box.width >= LABEL_WIDTH && box.height >= LABEL_HEIGHT;
          const maxChars = Math.max(1, Math.floor((box.width - 8) / 7));
          const fit = (text: string) => text.length > maxChars ? `${text.slice(0, maxChars - 1)}…` : text;
          return (
            <g
              key={box.key}
              className="viz-cell"
              tabIndex={0}
              role="button"
              aria-label={`${box.label}, ${format(box)}`}
              data-tooltip={`${box.label} · ${format(box)}`}
              onClick={() => onOpen(box.key)}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  onOpen(box.key);
                }
              }}
            >
              <rect
                x={box.x}
                y={box.y}
                width={box.width}
                height={box.height}
                fill={box.percent === null ? theme.line : divergingHeat(box.percent, span)}
                stroke={theme.bg}
              />
              {/* Below the threshold there is no text at all: half a
                  letter is noise, and the label is in the box's
                  `aria-label` and the row detail regardless. */}
              {roomy && (
                <>
                  <text
                    className="viz-cell-label"
                    x={box.x + 4}
                    y={box.y + 13}
                    fill={theme.fg}
                  >
                    {fit(box.label)}
                  </text>
                  {box.height >= LABEL_HEIGHT * 2 && (
                    <text
                      className="viz-tick"
                      x={box.x + 4}
                      y={box.y + 27}
                      fill={theme.fg}
                    >
                      {fit(format(box))}
                    </text>
                  )}
                </>
              )}
            </g>
          );
        })}
      </svg>
      {/* Always drawn. Colour on its own carries no value; without a
          scale a reader cannot tell a 2% day from a 20% one. */}
      <figcaption className="viz-legend" data-legend="heat">
        <span className="muted">−{span}%</span>
        <svg className="viz-ramp" viewBox="0 0 100 10" aria-hidden="true" preserveAspectRatio="none">
          {Array.from({ length: 21 }, (_, step) => {
            const percent = ((step - 10) / 10) * span;
            return (
              <rect
                key={step}
                x={step * 5}
                y={0}
                width={5}
                height={10}
                fill={divergingHeat(percent, span)}
              />
            );
          })}
        </svg>
        <span className="muted">+{span}%</span>
        <span className="muted">Area is size; colour is the move. Enter opens a box.</span>
      </figcaption>
    </figure>
  );
}
