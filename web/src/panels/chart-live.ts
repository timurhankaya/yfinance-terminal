// The live bar: what a chart shows between two REST loads.
//
// Folding each tick into a running candle, rather than recomputing the
// last bar from the newest tick alone, is what makes the high and low
// honest -- a bar that spiked to 233 and came back to 232 has to keep
// the 233.
import { useEffect, useMemo, useState } from "react";
import { useQuote } from "../live/hooks";
import { BucketMode, applyTick } from "./chart-data";
import type { Candle } from "./chart-data";

export interface LiveSeries {
  /** The archive's bars with the live bar folded in. */
  candles: Candle[];
  /** Increments when a tick opened a bucket the archive has no bar for.
   *  A caller refetches on it: the pipeline writes that bar within a
   *  batch or two, and the REST copy is the one with volume in it. */
  rolled: number;
}

/** `base` plus whatever the socket has said since it was loaded. */
export function useLiveSeries(
  base: Candle[],
  symbol: string | null,
  intervalSeconds: number,
  mode: BucketMode,
  live: boolean,
): LiveSeries {
  const quote = useQuote(live ? symbol : null);
  const [bar, setBar] = useState<Candle | null>(null);
  const [rolled, setRolled] = useState(0);

  // A fresh REST load supersedes whatever was folded on top of the old
  // one: those ticks are in the bars now, and keeping the running bar
  // would show a candle built from a stale open.
  useEffect(() => {
    setBar(null);
  }, [base]);

  useEffect(() => {
    if (!live || quote === undefined) return;
    setBar((current) => {
      const anchor = current ?? base[base.length - 1];
      const applied = applyTick(anchor, quote, intervalSeconds, mode);
      if (applied === null) return current;
      if (applied.isNew && anchor !== undefined && applied.candle.time !== anchor.time) {
        setRolled((count) => count + 1);
      }
      return applied.candle;
    });
  }, [quote, base, intervalSeconds, mode, live]);

  const candles = useMemo(() => {
    if (bar === null) return base;
    const last = base[base.length - 1];
    if (last !== undefined && last.time === bar.time) return [...base.slice(0, -1), bar];
    if (last !== undefined && bar.time < last.time) return base;
    return [...base, bar];
  }, [base, bar]);

  return { candles, rolled };
}
