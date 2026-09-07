// WLA: a watchlist, live.
//
// **The list lives in the URL.** `WLA AAPL MSFT NVDA` is
// `/ui/m/WLA?symbols=AAPL,MSFT,NVDA`, and that is the whole of its
// state. The terminal has no login and no user table (that is phase 2),
// so the alternatives were `localStorage` -- which the design says is
// for the entry redirect and "is not a second state source" -- or a
// server-side list nobody is authenticated to own. The URL is neither:
// it is shareable, it is a history entry like every other command, so
// Esc walks back through earlier lists, and it introduces no state the
// page has to keep in step with anything.
//
// Every row subscribes on its own. That is what makes a 200-symbol list
// affordable: the store keys quotes by symbol and each row selects only
// its own, so one symbol ticking re-renders one row. Measured and
// asserted -- `docs/measurements/websocket.md` ("Browser store at
// watchlist size") and `web/src/live/hooks.test.tsx`.
import type { ReactElement } from "react";
import { SYMBOL_RE } from "../commands/parser";
import { useGo } from "../commands/go";
import { Layout, type PanelArgs, type PanelProps, type PanelSpec } from "../commands/types";
import { useLinkState, useLiveEnabled, useQuote } from "../live/hooks";
import { LinkState, MarketHours } from "../live/types";
import { useListKeys } from "./common";
import { formatDecimal, formatInteger } from "./table";

//: The server's own per-connection ceiling (`ui/live.py`, MAX_SYMBOLS).
//: Asking for more would have the socket refuse the whole `sub` frame,
//: so the panel refuses the command instead and says which limit it hit.
export const WLA_MAX = 200;

export const WLA_ARGS = "WLA <symbol> [symbol ...]";
export const WLA_USAGE = `Usage: ${WLA_ARGS}  (up to ${WLA_MAX})`;

const SESSION_LABEL: Record<number, string> = {
  [MarketHours.PreMarket]: "pre",
  [MarketHours.Regular]: "open",
  [MarketHours.PostMarket]: "post",
  [MarketHours.ExtendedHours]: "ext",
};

/** The symbols in an args value, cleaned up.
 *
 *  Deduplicated because a repeated symbol is one subscription and would
 *  otherwise draw two rows that always agree, and capped because the
 *  socket refuses an oversized `sub` outright. */
export function parseSymbols(value: string | undefined): string[] {
  if (value === undefined) return [];
  const seen = new Set<string>();
  for (const token of value.split(",")) {
    const symbol = token.trim().toUpperCase();
    if (symbol !== "" && SYMBOL_RE.test(symbol)) seen.add(symbol);
  }
  return [...seen].slice(0, WLA_MAX);
}

function parseArgs(tokens: string[]): PanelArgs {
  if (tokens.length === 0) return {};
  if (tokens.length > WLA_MAX) throw new Error(WLA_USAGE);
  const symbols: string[] = [];
  for (const token of tokens) {
    const symbol = token.toUpperCase();
    // The command box is where a typo is cheapest to catch. A bad token
    // silently dropped here would be a row the reader asked for and
    // never got, with nothing saying why.
    if (!SYMBOL_RE.test(symbol)) throw new Error(`${token} is not a symbol. ${WLA_USAGE}`);
    symbols.push(symbol);
  }
  return { symbols: symbols.join(",") };
}

function Row({ symbol, selected }: { symbol: string; selected: boolean }): ReactElement {
  // One subscription per row, and one selector per row: this is the
  // hook whose isolation makes the whole panel affordable.
  const quote = useQuote(symbol);
  const change = quote?.c === undefined ? null : Number(quote.c);
  const move = change === null || !Number.isFinite(change) || change === 0
    ? undefined
    : change > 0 ? "up" : "down";
  return (
    <tr className={selected ? "row-selected" : undefined} aria-selected={selected}>
      <td className="strip-symbol">{symbol}</td>
      <td className="num">{quote === undefined ? "—" : formatDecimal(quote.p)}</td>
      <td className={move === undefined ? "num" : `num ${move}`}>
        {quote?.c === undefined ? "—" : formatDecimal(quote.c)}
      </td>
      <td className={move === undefined ? "num" : `num ${move}`}>
        {quote?.cp === undefined ? "—" : `${formatDecimal(quote.cp)}%`}
      </td>
      <td className="num">{quote?.v === undefined ? "—" : formatInteger(quote.v)}</td>
      <td>{quote === undefined ? "" : (SESSION_LABEL[quote.mh] ?? "?")}</td>
      <td>
        {/* Not "no data": a symbol outside `yfin stream scope` has no
            live path at all, and that is a configuration answer rather
            than a missing one. */}
        {quote === undefined ? (
          <span className="muted">not streamed</span>
        ) : (
          <span className="muted">{new Date(quote.t).toISOString().slice(11, 19)}</span>
        )}
      </td>
    </tr>
  );
}

export function WLA({ symbol, args }: PanelProps) {
  const go = useGo();
  const symbols = parseSymbols(args.symbols);
  // `WLA` with a symbol on the strip and no list of its own watches that
  // one: the shortest way in, and it keeps the command meaningful from
  // the function bar where there are no arguments to type.
  const watched = symbols.length > 0 ? symbols : symbol === null ? [] : [symbol];
  const enabled = useLiveEnabled();
  const link = useLinkState();
  const open = (index: number) => {
    const picked = watched[index];
    if (picked !== undefined) {
      go({ symbol: picked, code: "DES", args: {} });
    }
  };
  const [selected] = useListKeys(watched.length, open);

  if (watched.length === 0) {
    return (
      <section>
        <p className="muted">
          Nothing to watch yet. Type <code className="usage">{WLA_ARGS}</code> — the list is the
          URL, so it is shareable and Esc walks back through earlier ones.
        </p>
      </section>
    );
  }

  return (
    <section>
      <p className="chart-note">
        <span>
          {watched.length} {watched.length === 1 ? "symbol" : "symbols"} · UTC
        </span>
        {!enabled && <span className="muted">live stream off; these are last snapshots</span>}
        {enabled && link !== LinkState.Open && <span className="strip-link">reconnecting</span>}
      </p>
      <table className="grid" aria-label="watchlist">
        <thead>
          <tr>
            <th>Symbol</th>
            <th className="num">Price</th>
            <th className="num">Change</th>
            <th className="num">%</th>
            <th className="num">Volume</th>
            <th>Session</th>
            <th>Last</th>
          </tr>
        </thead>
        <tbody>
          {watched.map((code, index) => (
            <Row key={code} symbol={code} selected={index === selected} />
          ))}
        </tbody>
      </table>
      <p className="chart-legend">
        <span className="muted">
          j / k moves, Enter opens the selected symbol. Add or remove symbols by retyping the
          command.
        </span>
      </p>
    </section>
  );
}

export const WLA_PANEL: PanelSpec = {
  code: "WLA",
  title: "Watchlist: live prices for a list of symbols",
  usage: WLA_ARGS,
  needsSymbol: false,
  layout: Layout.Single,
  parseArgs,
  // A hand-edited URL can carry anything; `parseSymbols` is the one
  // place that decides what a list is, so the component reads a value
  // it can trust.
  normalizeArgs: (args) => {
    const symbols = parseSymbols(args.symbols);
    return symbols.length === 0 ? {} : { ...args, symbols: symbols.join(",") };
  },
  component: WLA,
};
