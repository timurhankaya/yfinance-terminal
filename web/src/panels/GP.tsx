// GP: the daily chart. Two years of candles, volume under them, and a
// marker wherever the company paid or split.
//
// Two years rather than "everything": that is the window a reader
// actually looks at on a daily chart, and the archive reaches back
// decades for symbols where it would otherwise be 12,000 candles of
// which 11,500 are a grey smear.
import { useMemo } from "react";
import { daysAgo, getActions, getBarsWindow, type Row } from "../api/client";
import type { PanelArgs, PanelProps, PanelSpec } from "../commands/types";
import { Layout } from "../commands/types";
import { Chart } from "./Chart";
import {
  BucketMode,
  INTERVAL_SECONDS,
  toCandles,
  toMarkers,
  toVolume,
} from "./chart-data";
import { useLiveSeries } from "./chart-live";
import { EmptyCard, ErrorCard, LoadState, MissingCard, usePanelData } from "./common";

//: One shared empty array, so "no rows yet" keeps its identity across
//: renders (see the memo below).
const NO_ROWS: Row[] = [];

export const GP_USAGE = "Usage: GP [years 1-10]";
const DEFAULT_YEARS = 2;
//: The API's own ceiling for a daily range is ~10 years.
const MAX_YEARS = 10;

function parseArgs(tokens: string[]): PanelArgs {
  if (tokens.length === 0) return { years: String(DEFAULT_YEARS) };
  const years = Number(tokens[0]);
  if (!Number.isInteger(years) || years < 1 || years > MAX_YEARS) throw new Error(GP_USAGE);
  return { years: String(years) };
}

interface Daily {
  bars: Row[];
  actions: Row[];
}

export function GP({ symbol, args }: PanelProps) {
  // Args can arrive from a hand-edited URL, not only from parseArgs.
  const raw = Number(args.years ?? DEFAULT_YEARS);
  const years = Number.isInteger(raw) && raw >= 1 && raw <= MAX_YEARS ? raw : DEFAULT_YEARS;
  const { state, retry } = usePanelData<Daily>(
    `${symbol ?? ""}|${years}`,
    async () => {
      if (symbol === null) throw new Error("no symbol");
      // One window, two reads: the markers have to line up with the
      // candles, so they are asked for together and drawn together.
      const from = daysAgo(Math.round(years * 365.25));
      const [bars, actions] = await Promise.all([
        getBarsWindow(symbol, "1d", from),
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
    INTERVAL_SECONDS["1d"] ?? 86_400,
    // A daily bar's instant is the session open, not UTC midnight, so
    // the live price extends today's bar and never opens tomorrow's.
    BucketMode.Session,
    true,
  );
  const volume = useMemo(() => toVolume(base, bars), [base, bars]);
  const markers = useMemo(() => toMarkers(actions, base), [actions, base]);

  if (symbol === null) return null;
  if (state.kind === LoadState.Loading) return <p className="muted">Loading {symbol} daily bars…</p>;
  if (state.kind === LoadState.Missing) return <MissingCard symbol={symbol} />;
  if (state.kind === LoadState.Error) return <ErrorCard message={state.message} onRetry={retry} />;
  if (state.kind === LoadState.Empty) return <EmptyCard what="daily bars" />;

  return (
    <section>
      <p className="chart-note">
        <span>
          {symbol} · daily · {years} {years === 1 ? "year" : "years"} · {candles.length} bars · UTC
        </span>
        <span className="muted">{GP_USAGE.slice(7)}</span>
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
  usage: GP_USAGE.slice(7),
  needsSymbol: true,
  layout: Layout.Headed,
  parseArgs,
  component: GP,
};
