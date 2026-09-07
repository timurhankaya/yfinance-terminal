// What a panel uses. Each hook owns one subscription for as long as the
// component is mounted, which is what makes the reference count in the
// store correct without any panel knowing about the socket.
import { useEffect } from "react";
import { useLive, release, retain, setTape } from "./store";
import type { LinkState, Tick } from "./types";

/** The last tick for one symbol, subscribing while the caller is mounted.
 *
 *  `undefined` until the snapshot lands -- which is also what a symbol
 *  nobody streams looks like, and the caller shows the archive in both
 *  cases rather than an empty price. */
export function useQuote(symbol: string | null): Tick | undefined {
  useEffect(() => {
    if (symbol === null) return;
    retain(symbol);
    return () => release(symbol);
  }, [symbol]);
  return useLive((state) => (symbol === null ? undefined : state.quotes[symbol]));
}

/** The tape for one symbol, newest first. Points the store's single ring
 *  at this symbol for as long as the caller is mounted. */
export function useTape(symbol: string | null): Tick[] {
  useEffect(() => {
    if (symbol === null) return;
    retain(symbol);
    setTape(symbol);
    return () => {
      release(symbol);
      setTape(null);
    };
  }, [symbol]);
  return useLive((state) => state.tape);
}

export function useLiveEnabled(): boolean {
  return useLive((state) => state.enabled);
}

export function useLinkState(): LinkState {
  return useLive((state) => state.link);
}

export function useDropped(): number {
  return useLive((state) => state.dropped);
}
