// The thin band under the function bar: what the symbol is doing right
// now. Present on every panel so the reader always knows which symbol
// the keyboard is pointed at; live only under a `headed` panel, which is
// what the layout field on `PanelSpec` decides.
//
// Everything it shows about the connection is stated, never implied. A
// price that has quietly stopped updating looks exactly like a quiet
// market, so "the stream is off" and "this tab lost its socket" each get
// their own words.
import type { ReactElement } from "react";
import { useDropped, useLinkState, useLiveEnabled, useQuote } from "../live/hooks";
import { LinkState, MarketHours } from "../live/types";
import type { Tick } from "../live/types";
import { formatDecimal } from "../panels/table";

const SESSION_LABEL: Record<MarketHours, string> = {
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

function Price({ tick }: { tick: Tick }): ReactElement {
  const move = direction(tick.c);
  return (
    <>
      <span className="strip-price">{formatDecimal(tick.p)}</span>
      {tick.c !== undefined && (
        <span className={move === undefined ? "strip-change" : `strip-change ${move}`}>
          {formatDecimal(tick.c)}
          {tick.cp !== undefined && ` (${formatDecimal(tick.cp)}%)`}
        </span>
      )}
      <span className="muted">{SESSION_LABEL[tick.mh as MarketHours] ?? "unknown session"}</span>
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
