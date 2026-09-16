// A price series in a table cell: no axes, no labels, no ticks. Not
// normalised: each is scaled to its own extent, so cells in different
// currencies sit side by side without pretending to be comparable.
// Memoised because `WLA` holds two hundred of these while every row
// re-renders on its own tick.
import { memo } from "react";
import type { ReactElement } from "react";
import { directionColor } from "./colors";
import { extent, linearScale } from "./scale";

export interface SparklineProps {
  /** Closes, oldest first. Fewer than two finite points draws nothing:
   *  the caller says what an absence means. */
  values: readonly number[];
  /** What a screen reader is told. The cell's own number is beside it,
   *  so this is the shape, not the value. */
  label: string;
  /** Fill under the line. Off in a table cell, where it would be ink
   *  without information at 18 pixels high. */
  area?: boolean;
}

//: The internal coordinate system. `preserveAspectRatio="none"` lets CSS
//: choose the box; `vector-effect` keeps the stroke a hairline anyway.
const WIDTH = 100;
const HEIGHT = 24;
const PAD = 2;

function SparklineBase({ values, label, area = false }: SparklineProps): ReactElement | null {
  const points = values.filter((value) => Number.isFinite(value));
  if (points.length < 2) return null;
  const span = extent(points);
  if (span === null) return null;

  const x = linearScale({ min: 0, max: points.length - 1 }, { min: PAD, max: WIDTH - PAD });
  const y = linearScale(span, { min: HEIGHT - PAD, max: PAD });
  const line = points.map((value, index) => `${x(index).toFixed(2)},${y(value).toFixed(2)}`);
  const path = `M${line.join("L")}`;
  const first = points[0] ?? 0;
  const last = points[points.length - 1] ?? 0;
  const colour = directionColor(last - first);

  return (
    <svg
      className="spark"
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={label}
    >
      {area && (
        <path
          d={`${path}L${(WIDTH - PAD).toFixed(2)},${HEIGHT}L${PAD.toFixed(2)},${HEIGHT}Z`}
          fill={colour}
          fillOpacity={0.15}
          stroke="none"
        />
      )}
      <path d={path} fill="none" stroke={colour} strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

export const Sparkline = memo(SparklineBase);
Sparkline.displayName = "Sparkline";
