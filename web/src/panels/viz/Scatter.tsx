// Two measures against each other, one dot per thing.
//
// The picture a table cannot make: whether the two move together at all.
// An optional reference line is what turns a cloud into a statement --
// "above this line the market pays more for the same growth".
import type { ReactElement } from "react";
import { useChartSize } from "./useChartSize";

import { useVizTheme } from "./useVizTheme";
import { extent, linearScale, niceTicks } from "./scale";

export interface ScatterPoint {
  key: string;
  label: string;
  x: number;
  y: number;
  /** Identity, when the dots belong to groups. Direction and emphasis
   *  are not this: a dot is a thing, not a rise. */
  colour?: string;
}

/** A line in DATA coordinates, so the caller states the relation rather
 *  than a pixel slope. */
export interface ScatterReference {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  label: string;
}

export interface ScatterProps {
  points: readonly ScatterPoint[];
  label: string;
  xLabel: string;
  yLabel: string;
  reference?: ScatterReference;
  format: (value: number) => string;
}

const MARGIN = { top: 10, right: 12, bottom: 30, left: 56 };


export function Scatter(props: ScatterProps): ReactElement | null {
  const { points, label, xLabel, yLabel, reference, format } = props;
  const theme = useVizTheme();
  const { ref, width: WIDTH, height: HEIGHT } = useChartSize(320);
  const PLOT = { width: WIDTH - MARGIN.left - MARGIN.right, height: HEIGHT - MARGIN.top - MARGIN.bottom };
  const usable = points.filter((p) => Number.isFinite(p.x) && Number.isFinite(p.y));
  const xSpan = extent(usable.map((p) => p.x));
  const ySpan = extent(usable.map((p) => p.y));
  if (xSpan === null || ySpan === null) return null;

  const x = linearScale(xSpan, { min: MARGIN.left, max: MARGIN.left + PLOT.width });
  const y = linearScale(ySpan, { min: MARGIN.top + PLOT.height, max: MARGIN.top });

  return (
    <figure className="viz-figure" ref={ref}>
      <svg className="viz" viewBox={`0 0 ${WIDTH} ${HEIGHT}`} width={WIDTH} height={HEIGHT} role="img" aria-label={label}>
        {niceTicks(ySpan.min, ySpan.max, 4).map((tick) => (
          <g key={`y${tick}`}>
            <line
              x1={MARGIN.left}
              x2={MARGIN.left + PLOT.width}
              y1={y(tick)}
              y2={y(tick)}
              stroke={theme.line}
            />
            <text
              className="viz-tick"
              x={MARGIN.left - 6}
              y={y(tick)}
              textAnchor="end"
              dominantBaseline="middle"
              fill={theme.muted}
            >
              {format(tick)}
            </text>
          </g>
        ))}
        {niceTicks(xSpan.min, xSpan.max, 5).map((tick) => (
          <text
            key={`x${tick}`}
            className="viz-tick"
            x={x(tick)}
            y={HEIGHT - 14}
            textAnchor="middle"
            fill={theme.muted}
          >
            {format(tick)}
          </text>
        ))}
        {reference !== undefined && (
          <line
            data-reference={reference.label}
            x1={x(reference.x1)}
            y1={y(reference.y1)}
            x2={x(reference.x2)}
            y2={y(reference.y2)}
            stroke={theme.accent}
            strokeDasharray="4 3"
            vectorEffect="non-scaling-stroke"
          />
        )}
        {usable.map((point) => (
          <circle
            key={point.key}
            cx={x(point.x)}
            cy={y(point.y)}
            r={3.5}
            fill={point.colour ?? theme.fg}
            fillOpacity={0.75}
          >
            <title>{point.label}</title>
          </circle>
        ))}
      </svg>
      <figcaption className="viz-legend">
        <span className="muted">
          x: {xLabel} · y: {yLabel}
          {reference !== undefined ? ` · dashed: ${reference.label}` : ""}
        </span>
      </figcaption>
    </figure>
  );
}
