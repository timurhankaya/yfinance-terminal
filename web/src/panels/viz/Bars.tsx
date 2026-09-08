// Categorical bars: a value per period, one or more series, on a shared
// zero baseline.
//
// Zero is always in the domain, and that is the whole point of a bar --
// its length IS the value, so a chart whose axis starts at the smallest
// bar draws a loss as a short rise. `lightweight-charts` is not this: it
// draws time, and a fiscal quarter is a category rather than an instant.
import type { ReactElement } from "react";
import { seriesColors } from "./colors";
import { useVizTheme } from "./useVizTheme";
import { extent, linearScale, niceTicks } from "./scale";

export interface BarSeries {
  key: string;
  label: string;
  /** One value per category, null where the archive has none. */
  values: ReadonlyArray<number | null>;
}

/** A second measure on its own scale, drawn as a line over the bars --
 *  a margin over revenue, say, where sharing the bars' axis would flatten
 *  it to nothing. */
export interface BarsLine {
  label: string;
  values: ReadonlyArray<number | null>;
}

/** A category worth pointing at: a quarter a company split its stock,
 *  say. A mark has no value of its own -- it says WHEN, which is why it
 *  sits on the axis rather than on the scale. */
export interface BarMark {
  category: string;
  label: string;
}

export interface BarsProps {
  categories: readonly string[];
  series: readonly BarSeries[];
  line?: BarsLine;
  marks?: readonly BarMark[];
  label: string;
  /** The axis tick text. Numbers are formatted by the caller, which is
   *  the only place that knows whether they are dollars or a count. */
  format: (value: number) => string;
}

const WIDTH = 640;
const HEIGHT = 260;
const MARGIN = { top: 10, right: 44, bottom: 26, left: 56 };
const PLOT = {
  width: WIDTH - MARGIN.left - MARGIN.right,
  height: HEIGHT - MARGIN.top - MARGIN.bottom,
};

const NO_MARKS: BarMark[] = [];

function finite(values: ReadonlyArray<number | null>): number[] {
  return values.filter((value): value is number => value !== null && Number.isFinite(value));
}

export function Bars(props: BarsProps): ReactElement | null {
  const { categories, series, line, marks = NO_MARKS, label, format } = props;
  const theme = useVizTheme();
  const all = series.flatMap((s) => finite(s.values));
  const span = extent([...all, 0]);
  if (span === null || categories.length === 0) return null;

  const colours = seriesColors(series.length);
  const y = linearScale(span, { min: MARGIN.top + PLOT.height, max: MARGIN.top });
  const band = PLOT.width / categories.length;
  //: A tenth of the band on each side, so neighbouring groups do not touch.
  const inner = band * 0.8;
  const barWidth = inner / Math.max(1, series.length);
  const zero = y(0);
  const ticks = niceTicks(span.min, span.max, 4);

  const lineValues = line === undefined ? [] : finite(line.values);
  const lineSpan = extent([...lineValues, 0]);
  const lineY =
    lineSpan === null
      ? null
      : linearScale(lineSpan, { min: MARGIN.top + PLOT.height, max: MARGIN.top });
  const linePoints =
    line === undefined || lineY === null
      ? []
      : line.values.flatMap((value, index) =>
          value === null || !Number.isFinite(value)
            ? []
            : [`${(MARGIN.left + band * (index + 0.5)).toFixed(2)},${lineY(value).toFixed(2)}`],
        );

  return (
    <figure className="viz-figure">
      <svg className="viz" viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label={label}>
        {ticks.map((tick) => (
          <g key={tick}>
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
        {categories.map((category, index) => (
          <text
            key={category}
            className="viz-tick"
            x={MARGIN.left + band * (index + 0.5)}
            y={HEIGHT - 8}
            textAnchor="middle"
            fill={theme.muted}
          >
            {category}
          </text>
        ))}
        {series.map((one, sIndex) =>
          one.values.map((value, index) => {
            if (value === null || !Number.isFinite(value)) return null;
            const top = Math.min(zero, y(value));
            const height = Math.abs(zero - y(value));
            return (
              <rect
                key={`${one.key}-${categories[index] ?? index}`}
                x={MARGIN.left + band * index + band * 0.1 + barWidth * sIndex}
                y={top}
                width={Math.max(1, barWidth - 1)}
                height={Math.max(1, height)}
                fill={colours[sIndex] ?? theme.muted}
              />
            );
          }),
        )}
        {marks.map((mark) => {
          const index = categories.indexOf(mark.category);
          if (index < 0) return null;
          const x = MARGIN.left + band * (index + 0.5);
          const y = HEIGHT - MARGIN.bottom + 4;
          // A triangle on the axis, not a bar: a split has no amount, and
          // drawn on the value scale it would claim one.
          return (
            <polygon
              key={`${mark.category}-${mark.label}`}
              points={`${x},${y} ${x - 4},${y + 7} ${x + 4},${y + 7}`}
              fill={theme.accent}
            >
              <title>{mark.label}</title>
            </polygon>
          );
        })}
        {linePoints.length > 1 && (
          <path
            d={`M${linePoints.join("L")}`}
            fill="none"
            stroke={theme.accent}
            strokeWidth={1.5}
            vectorEffect="non-scaling-stroke"
          />
        )}
      </svg>
      <figcaption className="viz-legend">
        {series.map((one, index) => (
          <span key={one.key}>
            <Swatch colour={colours[index] ?? theme.muted} /> {one.label}
          </span>
        ))}
        {line !== undefined && (
          <span>
            <Swatch colour={theme.accent} /> {line.label} (right)
          </span>
        )}
        {marks.length > 0 && (
          <span>
            <span aria-hidden="true">▲</span> {marks.length}{" "}
            {marks.length === 1 ? "mark" : "marks"} on the axis: {marks[0]?.label}
            {marks.length > 1 ? " and others" : ""}
          </span>
        )}
      </figcaption>
    </figure>
  );
}

/** A legend key. An `<svg>` rather than a styled `<span>`: colour goes on
 *  a `fill` attribute, and `web/src` writes no `style` (Decision 6). */
export function Swatch({ colour }: { colour: string }): ReactElement {
  return (
    <svg className="viz-swatch" viewBox="0 0 10 10" aria-hidden="true">
      <rect x="0" y="0" width="10" height="10" fill={colour} />
    </svg>
  );
}
