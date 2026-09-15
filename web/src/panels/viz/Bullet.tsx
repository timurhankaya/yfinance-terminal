// A range with a mark on it: what an analyst target looks like.
//
// Low, mean and high are three numbers about ONE thing, and three rows
// of a table make the reader do the arithmetic of "where is the price in
// that range" themselves. That distance is the whole question.
import type { ReactElement } from "react";
import { useChartSize } from "./useChartSize";

import { useVizTheme } from "./useVizTheme";
import { linearScale } from "./scale";

export interface BulletProps {
  low: number;
  high: number;
  /** The consensus, drawn as a tick inside the range. */
  mean: number;
  /** Where the thing actually is, or null when it is not known. */
  actual: number | null;
  label: string;
  format: (value: number) => string;
}

const HEIGHT = 56;
const PAD = 8;
const TRACK = { y: 18, height: 12 };

export function Bullet(props: BulletProps): ReactElement | null {
  const { low, high, mean, actual, label, format } = props;
  const theme = useVizTheme();
  const { ref, width: WIDTH } = useChartSize(HEIGHT);
  if (!Number.isFinite(low) || !Number.isFinite(high) || high < low) return null;

  const x = linearScale({ min: low, max: high }, { min: PAD, max: WIDTH - PAD });
  // Clipped, not dropped. A price above the highest target is exactly the
  // situation worth seeing, and a marker drawn off the track would be
  // invisible; it sits on the end and the label says which side.
  const outside = actual === null || !Number.isFinite(actual)
    ? null
    : actual < low
      ? "below"
      : actual > high
        ? "above"
        : null;
  const marker = actual === null || !Number.isFinite(actual)
    ? null
    : Math.min(WIDTH - PAD, Math.max(PAD, x(actual)));

  return (
    <figure className="viz-figure" ref={ref}>
      <svg className="viz" viewBox={`0 0 ${WIDTH} ${HEIGHT}`} width={WIDTH} height={HEIGHT} role="img" aria-label={label}>
        <rect
          x={PAD}
          y={TRACK.y}
          width={WIDTH - PAD * 2}
          height={TRACK.height}
          fill={theme.line}
        />
        <line
          x1={x(mean)}
          x2={x(mean)}
          y1={TRACK.y - 4}
          y2={TRACK.y + TRACK.height + 4}
          stroke={theme.muted}
          strokeWidth={2}
          vectorEffect="non-scaling-stroke"
        />
        {marker !== null && (
          <circle
            data-clipped={outside ?? undefined}
            cx={marker}
            cy={TRACK.y + TRACK.height / 2}
            r={5}
            fill={theme.accent}
          />
        )}
        <text className="viz-tick" x={PAD} y={HEIGHT - 8} textAnchor="start" fill={theme.muted}>
          {format(low)}
        </text>
        <text
          className="viz-tick"
          x={WIDTH - PAD}
          y={HEIGHT - 8}
          textAnchor="end"
          fill={theme.muted}
        >
          {format(high)}
        </text>
        <text className="viz-tick" x={x(mean)} y={12} textAnchor="middle" fill={theme.muted}>
          {format(mean)}
        </text>
      </svg>
      {outside !== null && (
        <figcaption className="viz-legend">
          <span className="muted">
            {format(actual ?? 0)} is {outside} the range; the marker sits on the end.
          </span>
        </figcaption>
      )}
    </figure>
  );
}
