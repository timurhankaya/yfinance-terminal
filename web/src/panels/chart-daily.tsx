// The daily chart's body: candles over a window of years, volume under
// them, and a marker wherever the company paid or split.
//
// Two years by default rather than "everything": that is the window a
// reader actually looks at, and the archive reaches back decades for
// symbols where it would otherwise be 12,000 candles of which 11,500
// are a grey smear.
//
// A body, not a panel. `GP` owns the code, the arguments and the
// controls; this owns one way of drawing a chart. The intraday body
// beside it is a different pipeline -- a different table, a different
// live bucket, a different axis, and `bar_gaps` instead of actions --
// which is why the two did not become one function with a flag.
import { useMemo } from "react";
import type { ReactElement } from "react";
import { INTERVALS, Interval, daysAgo, getActions, getBarsWindow, type Row } from "../api/client";
import { Chart } from "./Chart";
import { BucketMode, toCandles, toMarkers, toVolume } from "./chart-data";
import { useLiveSeries } from "./chart-live";
import { EmptyCard, ErrorCard, LoadState, MissingCard, usePanelData } from "./common";

//: One shared empty array, so "no rows yet" keeps its identity across
//: renders (see the memos below).
const NO_ROWS: Row[] = [];

/** The intervals this body draws: the ones the archive keeps a session
 *  close for (`price_history` daily, `periodic_bars` weekly and
 *  monthly). Everything shorter is the intraday body's. */
export const SESSION_INTERVALS: Interval[] = [Interval.D1, Interval.Wk1, Interval.Mo1];

interface Daily {
  bars: Row[];
  actions: Row[];
}

export function DailyChart(props: {
  symbol: string;
  interval: Interval;
  years: number;
}): ReactElement {
  const { symbol, interval, years } = props;
  const { state, retry } = usePanelData<Daily>(
    `${symbol}|${interval}|${years}`,
    async () => {
      // One window, two reads: the markers have to line up with the
      // candles, so they are asked for together and drawn together.
      const from = daysAgo(Math.round(years * 365.25));
      const [bars, actions] = await Promise.all([
        getBarsWindow(symbol, interval, from),
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
    INTERVALS[interval].seconds,
    // A session bar's instant is the open, not UTC midnight, so the live
    // price extends today's bar and never opens tomorrow's.
    BucketMode.Session,
  );
  const volume = useMemo(() => toVolume(base, bars), [base, bars]);
  const markers = useMemo(() => toMarkers(actions, base), [actions, base]);

  if (state.kind === LoadState.Loading) return <p className="muted">Loading {symbol} {interval} bars…</p>;
  if (state.kind === LoadState.Missing) return <MissingCard symbol={symbol} />;
  if (state.kind === LoadState.Error) return <ErrorCard message={state.message} onRetry={retry} />;
  if (state.kind === LoadState.Empty) return <EmptyCard what={`${interval} bars`} />;

  return (
    <>
      <p className="chart-note">
        <span>
          {symbol} · {interval} · {years} {years === 1 ? "year" : "years"} · {candles.length} bars · UTC
        </span>
      </p>
      <Chart
        candles={candles}
        volume={volume}
        markers={markers}
        whitespace={[]}
        band={[]}
        fitKey={`${symbol}|${interval}|${years}`}
        timeVisible={false}
        label={`${symbol} ${interval} candles`}
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
    </>
  );
}
