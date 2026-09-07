// The thin band under the function bar: what the symbol is doing right
// now. Present on every panel so the reader always knows which symbol
// the keyboard is pointed at; live only under a `headed` panel, which is
// what the layout field on `PanelSpec` decides.
//
// Everything it shows about the connection is stated, never implied. A
// price that has quietly stopped updating looks exactly like a quiet
// market, so "the stream is off" and "this tab lost its socket" each get
// their own words.
import { useEffect, useRef } from "react";
import type { ReactElement } from "react";
import { useDropped, useLinkState, useLiveEnabled, useQuote } from "../live/hooks";
import { LinkState, MarketHours } from "../live/types";
import type { Tick } from "../live/types";
import { formatDecimal } from "../panels/table";
import { formatPrice } from "../panels/format";

//: Keyed by `number`, not by `MarketHours`: `tick.mh` is whatever code
//: the wire carried, and asserting it into the enum would make the
//: fallback below dead code that TypeScript believes can never run.
const SESSION_LABEL: Record<number, string> = {
  [MarketHours.PreMarket]: "pre-market",
  [MarketHours.Regular]: "open",
  [MarketHours.PostMarket]: "after hours",
  [MarketHours.ExtendedHours]: "extended",
};

/** `HH:MM UTC` for a tick's own timestamp. UTC like the rest of the
 *  terminal: the archive keys everything by it and a reader in another
 *  city sees the same number. */
export function clock(epochMs: number): string {
  return `${new Date(epochMs).toISOString().slice(11, 16)} UTC`;
}

/** The sign of a change, as a class name. Neither when it is flat or
 *  absent -- a grey zero is a fact, a green one is a story. */
export function direction(change: string | undefined): string | undefined {
  if (change === undefined) return undefined;
  const value = Number(change);
  if (!Number.isFinite(value) || value === 0) return undefined;
  return value > 0 ? "up" : "down";
}

/** Which way the price moved against the previous tick, as a class
 *  name; undefined on the first tick and when it did not move. This is
 *  the flash's direction, distinct from the day's change: a price can
 *  tick up while still down on the day. */
export function tickMove(current: string, previous: string | undefined): string | undefined {
  if (previous === undefined) return undefined;
  const now = Number(current);
  const before = Number(previous);
  if (!Number.isFinite(now) || !Number.isFinite(before) || now === before) return undefined;
  return now > before ? "up" : "down";
}

function Price({ tick }: { tick: Tick }): ReactElement {
  const move = direction(tick.c);
  // The previous tick's price, for the flash. Read during render, written
  // after it: the comparison must see the tick before this one.
  const previous = useRef<string | undefined>(undefined);
  const flash = tickMove(tick.p, previous.current);
  useEffect(() => {
    previous.current = tick.p;
  }, [tick.p]);
  return (
    <>
      <span
        // Keyed by the tick's instant so every tick remounts the span and
        // the CSS animation plays again rather than once per page.
        key={tick.t}
        className={flash === undefined ? "strip-price" : `strip-price flash-${flash}`}
      >
        {formatPrice(tick.p)}
      </span>
      {tick.c !== undefined && (
        <span className={move === undefined ? "strip-change" : `strip-change ${move}`}>
          {formatPrice(tick.c)}
          {tick.cp !== undefined && ` (${formatDecimal(tick.cp)}%)`}
        </span>
      )}
      <span className="muted">{SESSION_LABEL[tick.mh] ?? "unknown session"}</span>
      <span className="muted">{clock(tick.t)}</span>
    </>
  );
}

export function Strip({ symbol, live }: { symbol: string; live: boolean }): ReactElement {
  const quote = useQuote(live ? symbol : null);
  const enabled = useLiveEnabled();
  const link = useLinkState();
  const dropped = useDropped();

  return (
    <div className="strip">
      <span className="strip-symbol">{symbol}</span>
      {live && quote !== undefined && <Price tick={quote} />}
      {live && quote === undefined && (
        // Not an error: a symbol outside `stream scope` has no live path
        // at all, and the archive panels below are still the whole point.
        <span className="muted">no live price</span>
      )}
      {live && !enabled && (
        <span className="muted">live stream off</span>
      )}
      {live && enabled && link !== LinkState.Open && (
        <span className="strip-link" title="reconnecting" aria-label="disconnected">
          ●
        </span>
      )}
      {live && dropped > 0 && (
        <span className="muted" title="ticks this connection could not keep up with">
          {dropped} dropped
        </span>
      )}
    </div>
  );
}
