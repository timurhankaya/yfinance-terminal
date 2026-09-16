// The sparkline column, shared by the three panels that draw one. The
// COLUMN is written per panel, since each writes its table its own way;
// the fetch is not: one request for the whole page, keyed by the symbol
// list. Here rather than in `viz/` because it calls the API, and the
// primitives stay pure.
import { useMemo } from "react";
import type { ReactNode } from "react";
import { SPARKLINE_MAX_SYMBOLS, SPARKLINE_POINTS, getSparklines } from "../api/client";
import type { SparklineSet } from "../api/client";
import { LoadState, usePanelData } from "./common";
import { asNumber } from "./format";
import { Sparkline } from "./viz";

/** The column heading. Short: the column is 80 pixels wide and the shape
 *  underneath says the rest. */
export const SPARK_LABEL = "Trend";

//: What a failed load says, once, in the heading -- never per row. The
//: rows themselves are fine: their prices come from the socket, and a
//: dead REST call must not make a live watchlist look broken.
export const SPARK_FAILED_LABEL = "Trend (unavailable)";

export interface SparkData {
  /** Closes per symbol, oldest first. Absent for a symbol the archive
   *  has no bars for. */
  bySymbol: ReadonlyMap<string, number[]>;
  points: number;
  loading: boolean;
  failed: boolean;
}

const EMPTY: SparkData = { bySymbol: new Map(), points: SPARKLINE_POINTS, loading: false, failed: false };

/** One request for a whole page of rows, keyed by the symbol list and
 *  point count. Above the route's cap the list is cut rather than
 *  refused: a 422 would take the column away from every row. */
export function useSparklines(symbols: string[], points: number = SPARKLINE_POINTS): SparkData {
  const wanted = symbols.slice(0, SPARKLINE_MAX_SYMBOLS);
  const key = `${wanted.join(",")}:${points}`;
  const { state } = usePanelData<SparklineSet | null>(key, () =>
    wanted.length === 0 ? Promise.resolve(null) : getSparklines(wanted, points),
  );
  return useMemo(() => {
    if (state.kind === LoadState.Error) return { ...EMPTY, points, failed: true };
    if (state.kind !== LoadState.Ready) return { ...EMPTY, points, loading: true };
    const set = state.data;
    if (set === null) return { ...EMPTY, points };
    const bySymbol = new Map<string, number[]>();
    for (const series of set.series) {
      // `asNumber`, because decimals arrive as strings; an SVG needs
      // numbers and a silent NaN would draw a line to nowhere.
      const closes = series.closes.flatMap((close) => {
        const value = asNumber(close);
        return value === null ? [] : [value];
      });
      bySymbol.set(series.symbol, closes);
    }
    return { bySymbol, points: set.points, loading: false, failed: false };
  }, [state, points]);
}

/** The heading, which is where a failure is reported. */
export function sparkLabel(data: SparkData): string {
  return data.failed ? SPARK_FAILED_LABEL : SPARK_LABEL;
}

/** One cell. Three states worth telling apart: still loading, no bars in
 *  the archive, and a shape. */
export function SparkCell({ data, symbol }: { data: SparkData; symbol: string }): ReactNode {
  const closes = data.bySymbol.get(symbol);
  if (closes === undefined) {
    if (data.loading || data.failed) return <span className="muted">—</span>;
    return <span className="muted">no data</span>;
  }
  return (
    <Sparkline
      values={closes}
      label={`${symbol}, ${closes.length} sessions, ${trend(closes)}`}
    />
  );
}

function trend(closes: readonly number[]): string {
  const first = closes[0];
  const last = closes[closes.length - 1];
  if (first === undefined || last === undefined || first === last) return "flat";
  return last > first ? "up" : "down";
}

