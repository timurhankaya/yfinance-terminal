// GIP: the intraday chart. Five sessions of candles at the interval you
// ask for, the last of them moving with the socket, and a shaded band
// wherever the archive knows it is missing bars.
//
// The band is the reason this panel needs a route of its own. An hour
// with no candles means one of two very different things -- the market
// was closed, or a fetch was missed -- and only `bar_gaps` can tell them
// apart. Without it the chart quietly draws a continuous line across a
// hole and the reader has no way to know.
import { useEffect, useMemo, useRef, useState } from "react";
import {
  INTERVALS,
  Interval,
  daysAgo,
  getBarsWindow,
  getGaps,
  isInterval,
  type GapRow,
  type Row,
} from "../api/client";
import { useLinkState } from "../live/hooks";
import { LinkState } from "../live/types";
import type { PanelArgs, PanelProps, PanelSpec } from "../commands/types";
import { Layout } from "../commands/types";
import { Chart } from "./Chart";
import { BucketMode, gapBands, toCandles, toVolume } from "./chart-data";
import { useLiveSeries } from "./chart-live";
import { EmptyCard, ErrorCard, LoadState, MissingCard, useKeptData, usePanelData } from "./common";

//: Shared empties, so "no rows yet" keeps its identity across renders.
const NO_ROWS: Row[] = [];
const NO_GAPS: GapRow[] = [];

//: The subset of the interval vocabulary the intraday route serves.
export const INTRADAY_INTERVALS: Interval[] = [Interval.M1, Interval.M5, Interval.M15, Interval.M60];
export const GIP_ARGS = `GIP [${INTRADAY_INTERVALS.join("|")}]`;
export const GIP_USAGE = `Usage: ${GIP_ARGS}`;
const DEFAULT_INTERVAL = Interval.M5;

function intradayOr(value: string | undefined, fallback: Interval): Interval {
  if (value === undefined || !isInterval(value)) return fallback;
  return INTRADAY_INTERVALS.includes(value) ? value : fallback;
}

//: Five trading sessions, asked for as nine calendar days: a week has
//: two weekend days in it and a holiday costs one more. Asking by
//: sessions is not possible -- that is the exchange's calendar, which
//: this page does not have.
const WINDOW_DAYS = 9;

function parseArgs(tokens: string[]): PanelArgs {
  const asked = tokens[0] ?? DEFAULT_INTERVAL;
  if (!isInterval(asked) || !INTRADAY_INTERVALS.includes(asked)) throw new Error(GIP_USAGE);
  return { interval: asked };
}

interface Intraday {
  bars: Row[];
  gaps: GapRow[];
}

export function GIP({ symbol, args }: PanelProps) {
  const interval = intradayOr(args.interval, DEFAULT_INTERVAL);
  const link = useLinkState();
  // Bumped to refetch: when the live bar rolls into a bucket the archive
  // has not written yet, and when the socket comes back after a gap in
  // this page's own coverage.
  const [reload, setReload] = useState(0);

  const { state, retry } = usePanelData<Intraday>(
    `${symbol ?? ""}|${interval}|${reload}`,
    async () => {
      if (symbol === null) throw new Error("no symbol");
      const from = daysAgo(WINDOW_DAYS);
      // `session=regular` matches what the live bar is built from: a
      // series with extended-hours bars in it cannot be extended by
      // regular-session ticks without a step at every open.
      const [bars, gaps] = await Promise.all([
        getBarsWindow(symbol, interval, from, "regular"),
        getGaps(symbol, interval, from),
      ]);
      return { bars, gaps };
    },
    (data) => data.bars.length === 0,
  );

  // A roll or a reconnect refetches the SAME window, and the chart has
  // to survive it: `Chart` builds its canvas once on purpose, so an
  // unmount throws away the reader's pan and zoom -- every five minutes
  // on a 5m chart. The reload is not part of this key, so only a new
  // symbol or interval clears what is on screen.
  const data = useKeptData(`${symbol ?? ""}|${interval}`, state);

  // Memoised, not a fresh `[]` per render: `base` is a dependency of the
  // live series, and a new identity every render would reset the running
  // candle before a single tick could be folded into it.
  const bars = useMemo(() => data?.bars ?? NO_ROWS, [data]);
  const gaps = useMemo(() => data?.gaps ?? NO_GAPS, [data]);
  const step = INTERVALS[interval].seconds;
  const base = useMemo(() => toCandles(bars), [bars]);
  const { candles, rolledAt } = useLiveSeries(base, symbol, step, BucketMode.Interval);
  const volume = useMemo(() => toVolume(base, bars), [base, bars]);
  const bands = useMemo(() => gapBands(gaps, step, base), [gaps, step, base]);

  // A rolled bucket means the archive is about to have that bar, with
  // the volume this page cannot know. One refetch per bucket: the ref
  // remembers which bucket was already fetched for, so the tick that
  // re-opens it on the fresh base does not fetch again.
  const fetchedFor = useRef<number | null>(null);
  useEffect(() => {
    if (rolledAt === null || rolledAt === fetchedFor.current) return;
    fetchedFor.current = rolledAt;
    setReload((count) => count + 1);
  }, [rolledAt]);

  // A RE-connect leaves a hole this page cannot fill from ticks: what
  // arrived while the socket was down was never delivered. The archive
  // has it. The first Open is not a reconnect: the page just loaded.
  const wasClosed = useRef(false);
  useEffect(() => {
    if (link === LinkState.Closed) wasClosed.current = true;
    if (link === LinkState.Open && wasClosed.current) {
      wasClosed.current = false;
      setReload((count) => count + 1);
    }
  }, [link]);

  if (symbol === null) return null;
  if (state.kind === LoadState.Missing) return <MissingCard symbol={symbol} />;
  if (state.kind === LoadState.Error) return <ErrorCard message={state.message} onRetry={retry} />;
  if (state.kind === LoadState.Empty) return <EmptyCard what={`${interval} bars`} />;
  // Only the FIRST load has nothing to show; a refresh renders the
  // payload it is refreshing.
  if (data === null) {
    return <p className="muted">Loading {symbol} {interval} bars…</p>;
  }

  return (
    <section>
      <p className="chart-note">
        <span>
          {symbol} · {interval} · last {WINDOW_DAYS} days · regular session · {candles.length} bars
          · UTC
        </span>
        <span className="muted">{GIP_ARGS}</span>
      </p>
      <Chart
        candles={candles}
        volume={volume}
        markers={[]}
        whitespace={bands.whitespace}
        band={bands.band}
        timeVisible
        label={`${symbol} ${interval} candles`}
      />
      <p className="chart-legend">
        {gaps.length > 0 ? (
          <>
            <span>
              <span className="swatch swatch-gap" />
              {gaps.length} open {gaps.length === 1 ? "gap" : "gaps"}: bars the archive is missing,
              not a closed market
            </span>
            {bands.truncated && (
              <span className="muted">
                too wide to shade in full; the count above is complete
              </span>
            )}
          </>
        ) : (
          <span className="muted">no open gaps in this window</span>
        )}
      </p>
    </section>
  );
}

export const GIP_PANEL: PanelSpec = {
  code: "GIP",
  title: "Intraday candles with the archive's gaps shaded",
  usage: GIP_ARGS,
  needsSymbol: true,
  layout: Layout.Headed,
  parseArgs,
  // Args can arrive from a hand-edited URL, not only from parseArgs.
  normalizeArgs: (args) => ({ ...args, interval: intradayOr(args.interval, DEFAULT_INTERVAL) }),
  component: GIP,
};
