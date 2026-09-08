// GP: the daily chart. Two years of candles, volume under them, and a
// marker wherever the company paid or split.
//
// Two years rather than "everything": that is the window a reader
// actually looks at on a daily chart, and the archive reaches back
// decades for symbols where it would otherwise be 12,000 candles of
// which 11,500 are a grey smear.
import { useMemo } from "react";
import { INTERVALS, Interval, daysAgo, getActions, getBarsWindow, type Row } from "../api/client";
import type { PanelArgs, PanelProps, PanelSpec } from "../commands/types";
import { Layout } from "../commands/types";
import { Chart } from "./Chart";
import { BucketMode, toCandles, toMarkers, toVolume } from "./chart-data";
import { useLiveSeries } from "./chart-live";
import { EmptyCard, ErrorCard, LoadState, MissingCard, usePanelData } from "./common";
import { Controls, NumberArg, useArgs } from "./controls";

//: One shared empty array, so "no rows yet" keeps its identity across
//: renders (see the memo below).
const NO_ROWS: Row[] = [];

const DEFAULT_YEARS = 2;
//: The API's own ceiling for a daily range is ~10 years.
const MAX_YEARS = 10;

export const GP_ARGS = `GP [years 1-${MAX_YEARS}]`;
export const GP_USAGE = `Usage: ${GP_ARGS}`;

function inRange(years: number): boolean {
  return Number.isInteger(years) && years >= 1 && years <= MAX_YEARS;
}

function parseArgs(tokens: string[]): PanelArgs {
  if (tokens.length === 0) return { years: String(DEFAULT_YEARS) };
  const years = Number(tokens[0]);
  if (!inRange(years)) throw new Error(GP_USAGE);
  return { years: String(years) };
}

interface Daily {
  bars: Row[];
  actions: Row[];
}

export function GP({ symbol, args }: PanelProps) {
  const years = Number(args.years ?? DEFAULT_YEARS);
  const set = useArgs("GP", symbol, args);
  const { state, retry } = usePanelData<Daily>(
    `${symbol ?? ""}|${years}`,
    async () => {
      if (symbol === null) throw new Error("no symbol");
      // One window, two reads: the markers have to line up with the
      // candles, so they are asked for together and drawn together.
      const from = daysAgo(Math.round(years * 365.25));
      const [bars, actions] = await Promise.all([
        getBarsWindow(symbol, Interval.D1, from),
        getActions(symbol),
      ]);
      return { bars, actions: actions.rows };
    },
    (data) => data.bars.length === 0,
  );

  // Memoised, not a fresh `[]` per render: `base` is a dependency of the
  // live series, and a new identity every render would reset the running
  // candle before a single tick could be folded into it.
  const bars = useMemo(() => (state.kind === LoadState.Ready ? state.data.bars : NO_ROWS), [state]);
  const actions = useMemo(
    () => (state.kind === LoadState.Ready ? state.data.actions : NO_ROWS),
    [state],
  );
  const base = useMemo(() => toCandles(bars), [bars]);
  const { candles } = useLiveSeries(
    base,
    symbol,
    INTERVALS[Interval.D1].seconds,
    // A daily bar's instant is the session open, not UTC midnight, so
    // the live price extends today's bar and never opens tomorrow's.
    BucketMode.Session,
  );
  const volume = useMemo(() => toVolume(base, bars), [base, bars]);
  const markers = useMemo(() => toMarkers(actions, base), [actions, base]);

  if (symbol === null) return null;
  // The controls are the panel's, not its ready state's: an interval
  // with no bars is exactly when a reader needs to pick another one.
  const controls = (
    <Controls>
      <NumberArg
          label="Window"
          value={years}
          min={1}
          max={MAX_YEARS}
          onSet={(next) => set({ years: String(next) })}
          suffix={years === 1 ? "year" : "years"}
        />
    </Controls>
  );

  if (state.kind === LoadState.Loading)
    return (
      <section>
        {controls}
        <p className="muted">Loading {symbol} daily bars…</p>
      </section>
    );
  if (state.kind === LoadState.Missing)
    return (
      <section>
        {controls}
        <MissingCard symbol={symbol} />
      </section>
    );
  if (state.kind === LoadState.Error)
    return (
      <section>
        {controls}
        <ErrorCard message={state.message} onRetry={retry} />
      </section>
    );
  if (state.kind === LoadState.Empty)
    return (
      <section>
        {controls}
        <EmptyCard what="daily bars" />
      </section>
    );

  return (
    <section>
      {controls}
      <p className="chart-note">
        <span>
          {symbol} · daily · {years} {years === 1 ? "year" : "years"} · {candles.length} bars · UTC
        </span>
        <span className="muted">{GP_ARGS}</span>
      </p>
      <Chart
        candles={candles}
        volume={volume}
        markers={markers}
        whitespace={[]}
        band={[]}
        timeVisible={false}
        label={`${symbol} daily candles`}
      />
      {markers.length > 0 && (
        <p className="chart-legend">
          <span>● dividend</span>
          <span>■ split</span>
          <span>▲ capital gain</span>
          <span className="muted">
            {markers.length} corporate {markers.length === 1 ? "action" : "actions"} in this window
          </span>
        </p>
      )}
    </section>
  );
}

export const GP_PANEL: PanelSpec = {
  code: "GP",
  title: "Daily candles, volume and corporate actions",
  usage: GP_ARGS,
  needsSymbol: true,
  layout: Layout.Headed,
  parseArgs,
  // Args can arrive from a hand-edited URL, not only from parseArgs.
  normalizeArgs: (args) => {
    const years = Number(args.years ?? DEFAULT_YEARS);
    return { ...args, years: String(inRange(years) ? years : DEFAULT_YEARS) };
  },
  component: GP,
};
