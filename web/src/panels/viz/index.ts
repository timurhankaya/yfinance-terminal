// The drawing primitives, and nothing that fetches.
//
// Each is a pure component: data in, SVG out. None of them reads the
// live store, calls the API or knows what a `PanelSpec` is -- which is
// what lets a panel decide where its numbers come from and this
// directory decide only what they look like.
//
// No charting library (spec, "Kararlar" 1). `lightweight-charts` stays
// the one way a TIME SERIES is drawn, and `Chart.tsx` stays the one file
// that touches it; these cover what it does not -- a treemap, categorical
// bars, a range with a mark, a scatter, and a line inside a table cell.
export { Bars, Swatch } from "./Bars";
export type { BarMark, BarSeries, BarsLine, BarsProps } from "./Bars";
export { Bullet } from "./Bullet";
export type { BulletProps } from "./Bullet";
export { Scatter } from "./Scatter";
export type { ScatterPoint, ScatterProps, ScatterReference } from "./Scatter";
export { Sparkline } from "./Sparkline";
export type { SparklineProps } from "./Sparkline";
export { MAX_CELLS, OTHER_KEY, Treemap, capCells, squarify } from "./Treemap";
export type { TreemapBox, TreemapItem, TreemapProps } from "./Treemap";
export {
  FALLBACK,
  GROUP_ORDER,
  Group,
  directionColor,
  divergingHeat,
  groupColor,
  mix,
  resetVizTheme,
  seriesColors,
  vizTheme,
} from "./colors";
export type { VizTheme } from "./colors";
export { extent, linearScale, niceTicks, normalize100 } from "./scale";
export type { Extent } from "./scale";
