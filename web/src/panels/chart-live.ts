// The live bar: what a chart shows between two REST loads. Each tick is
// folded into a running candle rather than recomputed from the newest
// tick alone, so the high and low survive.
import { useEffect, useMemo, useRef, useState } from "react";
import { useQuote } from "../live/hooks";
import { BucketMode, applyTick } from "./chart-data";
import type { Candle } from "./chart-data";

export interface LiveSeries {
  /** The archive's bars with the live bar folded in. */
  candles: Candle[];
  /** The open time of the newest bucket a tick opened beyond the
   *  archive's last bar, or null while no tick has; a caller refetches
   *  when it moves to a LATER bucket. A bucket time, not a counter: after
   *  the refetch the same tick opens the same bucket again, and a counter
   *  would count that as a second roll -- a refetch loop. */
  rolledAt: number | null;
}

/** `base` plus whatever the socket has said since it was loaded. */
export function useLiveSeries(
  base: Candle[],
  symbol: string | null,
  intervalSeconds: number,
  mode: BucketMode,
): LiveSeries {
  const quote = useQuote(symbol);
  const [bar, setBar] = useState<Candle | null>(null);
  const [rolledAt, setRolledAt] = useState<number | null>(null);
  // A mirror of `bar`, so the effect below can read the running candle
  // without reading state inside a `setBar` updater. React double-invokes
  // updaters in StrictMode, so a `setRolledAt` inside one would fire twice,
  // and the caller turns every roll into a window refetch.
  const barRef = useRef<Candle | null>(bar);
  barRef.current = bar;

  // A fresh REST load supersedes whatever was folded on top of the old
  // one: those ticks are in the bars now, and keeping the running bar
  // would show a candle built from a stale open.
  useEffect(() => {
    setBar(null);
    barRef.current = null;
  }, [base]);

  useEffect(() => {
    if (quote === undefined) return;
    const anchor = barRef.current ?? base[base.length - 1];
    const applied = applyTick(anchor, quote, intervalSeconds, mode);
    if (applied === null) return;
    if (applied.isNew && anchor !== undefined && applied.candle.time !== anchor.time) {
      const opened = applied.candle.time;
      setRolledAt((previous) => (previous === null || opened > previous ? opened : previous));
    }
    barRef.current = applied.candle;
    setBar(applied.candle);
  }, [quote, base, intervalSeconds, mode]);

  const candles = useMemo(() => {
    if (bar === null) return base;
    const last = base[base.length - 1];
    if (last !== undefined && last.time === bar.time) return [...base.slice(0, -1), bar];
    if (last !== undefined && bar.time < last.time) return base;
    return [...base, bar];
  }, [base, bar]);

  return { candles, rolledAt };
}
