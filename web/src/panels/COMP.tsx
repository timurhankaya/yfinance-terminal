// COMP: several symbols on one axis, each indexed to 100 at its own
// first session.
//
// The one question a table of prices cannot answer: which of these went
// further. Prices in different currencies at different magnitudes have
// no common axis -- a $600 stock and a $12 one drawn together is one
// line and a flat smear -- so what is drawn is the RATIO to where each
// series began.
//
// `single`, not `headed`, and that is deliberate. A headed panel opens a
// live subscription for the strip's symbol, and this page has no one
// symbol: `COMP AAPL MSFT` shared as a link would carry whichever symbol
// happened to be on the strip when it was typed. `WLA` settled the same
// question the same way.
//
// Seven symbols, because there are seven group colours and an eighth is
// not distinguishable -- the limit is legibility, not arithmetic.
import { useMemo } from "react";
import type { ReactElement } from "react";
import { Interval, daysAgo, getBarsWindow, ApiError, type Row } from "../api/client";
import { SYMBOL_RE } from "../commands/parser";
import { Layout, type PanelArgs, type PanelProps, type PanelSpec } from "../commands/types";
import { LineChart, type LineSeriesSpec } from "./Chart";
import { toComparison, type ComparisonSeries } from "./chart-data";
import { EmptyCard, ErrorCard, LoadState, usePanelData } from "./common";
import { LOCALE } from "./format";
import { GROUP_ORDER, Swatch, seriesColors } from "./viz";

/** The windows a comparison is asked over.
 *
 *  No `5y` and no `max`: `/v1/.../bars` pages at 1,000 rows, five years
 *  of daily closes is ~1,250, and seven symbols would be fourteen pages
 *  to answer a question three years already answers. */
export enum CompPeriod {
  SixMonth = "6m",
  Year = "1y",
  ThreeYear = "3y",
}

const PERIOD_DAYS: Record<CompPeriod, number> = {
  [CompPeriod.SixMonth]: 183,
  [CompPeriod.Year]: 365,
  [CompPeriod.ThreeYear]: 1096,
};

const PERIOD_LABEL: Record<CompPeriod, string> = {
  [CompPeriod.SixMonth]: "6 months",
  [CompPeriod.Year]: "1 year",
  [CompPeriod.ThreeYear]: "3 years",
};

const PERIODS = Object.values(CompPeriod);

export const DEFAULT_PERIOD = CompPeriod.Year;

//: Seven group colours; the eighth cannot be told from one of the seven.
export const COMP_MAX = GROUP_ORDER.length;

export const COMP_ARGS = `COMP <symbol> [symbol ...] [${PERIODS.join("|")}]`;
export const COMP_USAGE = `Usage: ${COMP_ARGS}  (up to ${COMP_MAX} symbols)`;

function isPeriod(token: string): token is CompPeriod {
  return (PERIODS as string[]).includes(token);
}

/** The symbols in an args value, cleaned up but NOT cut.
 *
 *  Unlike `WLA`, whose cap is the socket's and cannot be exceeded at
 *  all, this one is a limit on what a reader can tell apart. So the
 *  extra symbols survive normalisation and the component drops them
 *  where it can say so. */
export function parseSymbols(value: string | undefined): string[] {
  if (value === undefined) return [];
  const seen = new Set<string>();
  for (const token of value.split(",")) {
    const symbol = token.trim().toUpperCase();
    if (symbol !== "" && SYMBOL_RE.test(symbol)) seen.add(symbol);
  }
  return [...seen];
}

function parseArgs(tokens: string[]): PanelArgs {
  const rest = [...tokens];
  const last = rest[rest.length - 1]?.toLowerCase();
  const period = last !== undefined && isPeriod(last) ? (rest.pop(), last) : DEFAULT_PERIOD;
  if (rest.length === 0) throw new Error(COMP_USAGE);
  // Refused rather than cut: the command box is where the reader can
  // still fix it, and eight lines in seven colours is a chart that lies
  // about which is which.
  if (rest.length > COMP_MAX) throw new Error(COMP_USAGE);
  const symbols: string[] = [];
  for (const token of rest) {
    const symbol = token.toUpperCase();
    if (!SYMBOL_RE.test(symbol)) throw new Error(`${token} is not a symbol. ${COMP_USAGE}`);
    symbols.push(symbol);
  }
  return { symbols: symbols.join(","), period };
}

interface Comparison {
  series: ComparisonSeries[];
  /** Symbols the archive has no usable window for. */
  missing: string[];
}

/** One symbol's window, or null when the archive has none of it.
 *
 *  A 404 and an empty window are both "no bars for this symbol" and are
 *  reported as such. Anything else -- a 500, a timeout -- is left to
 *  reject: a failure the reader can retry must not be shown as a symbol
 *  that does not exist. */
async function loadOne(symbol: string, from: string): Promise<ComparisonSeries | null> {
  let rows: Row[];
  try {
    rows = await getBarsWindow(symbol, Interval.D1, from);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
  return toComparison(symbol, rows);
}

export function COMP({ args }: PanelProps): ReactElement {
  const asked = parseSymbols(args.symbols);
  // Not memoised, and it does not need to be: what identifies the query
  // is the joined string below, and `usePanelData` holds the loader in a
  // ref rather than in a dependency.
  const symbols = asked.slice(0, COMP_MAX);
  const dropped = asked.length - symbols.length;
  const period = args.period !== undefined && isPeriod(args.period) ? args.period : DEFAULT_PERIOD;

  const { state, retry } = usePanelData<Comparison>(
    `${symbols.join(",")}|${period}`,
    async () => {
      const from = daysAgo(PERIOD_DAYS[period]);
      const loaded = await Promise.all(symbols.map((symbol) => loadOne(symbol, from)));
      const series: ComparisonSeries[] = [];
      const missing: string[] = [];
      symbols.forEach((symbol, index) => {
        const one = loaded[index];
        if (one === null || one === undefined) missing.push(symbol);
        else series.push(one);
      });
      return { series, missing };
    },
    (data) => data.series.length === 0,
  );

  const series = state.kind === LoadState.Ready ? state.data.series : EMPTY_SERIES;
  // Colour by POSITION in what is actually drawn, so dropping a symbol
  // with no bars does not leave a hole in the palette.
  const colours = useMemo(() => seriesColors(series.length), [series.length]);
  const lines = useMemo<LineSeriesSpec[]>(
    () =>
      series.map((one, index) => ({
        key: one.symbol,
        label: one.symbol,
        colour: colours[index] ?? "",
        points: one.points,
      })),
    [series, colours],
  );

  if (symbols.length === 0) {
    return (
      <section>
        <p className="muted">
          Nothing to compare yet. Type <code className="usage">{COMP_ARGS}</code> — every series is
          indexed to 100 at its own first session, so the lines answer &quot;which went
          further&quot; rather than &quot;which costs more&quot;.
        </p>
      </section>
    );
  }
  if (state.kind === LoadState.Loading) return <p className="muted">Loading {symbols.length} series…</p>;
  if (state.kind === LoadState.Error) return <ErrorCard message={state.message} onRetry={retry} />;
  if (state.kind === LoadState.Empty) {
    return (
      <section>
        <EmptyCard what={`daily bars for ${symbols.join(", ")}`} />
      </section>
    );
  }
  if (state.kind !== LoadState.Ready) return <p className="muted">Loading…</p>;

  return (
    <section>
      <p className="chart-note">
        <span>
          {series.length} {series.length === 1 ? "series" : "series"} · {PERIOD_LABEL[period]} ·
          indexed to 100 · UTC
        </span>
        <span className="muted">{COMP_ARGS}</span>
      </p>
      <LineChart
        series={lines}
        baseline={100}
        label={`${symbols.join(", ")} indexed to 100 over ${PERIOD_LABEL[period]}`}
      />
      <p className="chart-legend">
        {series.map((one, index) => (
          <span key={one.symbol}>
            <Swatch colour={colours[index] ?? ""} /> {one.symbol}{" "}
            <span className={one.changePercent >= 0 ? "up" : "down"}>
              {one.changePercent >= 0 ? "+" : ""}
              {one.changePercent.toLocaleString(LOCALE, { maximumFractionDigits: 1 })}%
            </span>{" "}
            <span className="muted">from {isoDate(one.baseTime)}</span>
          </span>
        ))}
      </p>
      {state.data.missing.length > 0 && (
        <p className="chart-legend">
          <span className="muted">No bars in this window: {state.data.missing.join(", ")}</span>
        </p>
      )}
      {dropped > 0 && (
        <p className="chart-legend">
          <span className="warn">
            {dropped} more {dropped === 1 ? "symbol was" : "symbols were"} left out: seven colours
            are all a reader can tell apart.
          </span>
        </p>
      )}
      <p className="chart-legend">
        <span className="muted">
          Each line starts at 100 on its own first session, which is named beside it — two lines
          starting together do not mean they started on the same day.
        </span>
      </p>
    </section>
  );
}

const EMPTY_SERIES: ComparisonSeries[] = [];

/** The session a series was indexed on, as a date. */
function isoDate(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toISOString().slice(0, 10);
}

export const COMP_PANEL: PanelSpec = {
  code: "COMP",
  title: "Compare symbols: daily closes indexed to 100",
  usage: COMP_ARGS,
  needsSymbol: false,
  layout: Layout.Single,
  parseArgs,
  // A hand-edited URL can carry anything, including an eighth symbol.
  // The cut happens in the component, which is the only place that can
  // say it happened.
  normalizeArgs: (args) => {
    const symbols = parseSymbols(args.symbols);
    const period = args.period !== undefined && isPeriod(args.period) ? args.period : DEFAULT_PERIOD;
    return symbols.length === 0 ? { period } : { ...args, symbols: symbols.join(","), period };
  },
  component: COMP,
};
