// WLA: a watchlist, live. The list lives in the URL and nowhere else:
// `WLA AAPL MSFT NVDA` is `/ui/m/WLA?symbols=AAPL,MSFT,NVDA`, shareable
// and a history entry like any other command. Every row subscribes on
// its own, so one symbol ticking re-renders one row (asserted in
// `web/src/live/hooks.test.tsx`).
import { useState, type ReactElement } from "react";
import { SYMBOL_RE } from "../commands/parser";
import { usePanelRun } from "../workspace/frame";
import { Layout, type PanelArgs, type PanelProps, type PanelSpec } from "../commands/types";
import { useLinkState, useLiveEnabled, useQuote } from "../live/hooks";
import { LinkState, MarketHours } from "../live/types";
import { useListKeys } from "./common";
import { SparkCell, sparkLabel, useSparklines } from "./spark";
import type { SparkData } from "./spark";
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

function Row(
  { symbol, selected, spark, onOpen, onRemove }: {
    symbol: string; selected: boolean; spark: SparkData;
    onOpen: () => void; onRemove: () => void;
  },
): ReactElement {
  // One subscription per row, and one selector per row: this is the
  // hook whose isolation makes the whole panel affordable.
  const quote = useQuote(symbol);
  const closes = spark.bySymbol.get(symbol);
  const close = closes?.at(-1);
  const previous = closes?.at(-2);
  const archiveChange = close !== undefined && previous !== undefined ? close - previous : undefined;
  const price = quote?.p ?? close;
  const delta = quote === undefined ? archiveChange : quote.c;
  const percent = quote === undefined
    ? archiveChange !== undefined && previous !== undefined && previous !== 0 ? archiveChange / previous * 100 : undefined
    : quote.cp;
  const change = delta === undefined ? null : Number(delta);
  const move = change === null || !Number.isFinite(change) || change === 0
    ? undefined
    : change > 0 ? "up" : "down";
  return (
    <tr className={selected ? "row-selected" : undefined} aria-selected={selected}>
      <td className="strip-symbol"><button type="button" onClick={onOpen} aria-label={`Open ${symbol}`}>{symbol}</button></td>
      <td className="num">{price === undefined ? "—" : formatDecimal(price)}</td>
      <td className={move === undefined ? "num" : `num ${move}`}>
        {delta === undefined ? "—" : formatDecimal(delta)}
      </td>
      <td className={move === undefined ? "num" : `num ${move}`}>
        {percent === undefined ? "—" : `${formatDecimal(percent)}%`}
      </td>
      <td className="num">{quote?.v === undefined ? "—" : formatInteger(quote.v)}</td>
      {/* The one cell on the row that does NOT come from the socket. It
          is a month of closes, so a tick cannot change it -- and does
          not redraw it either: `Sparkline` is memoised and `closes` is
          the same array across renders. */}
      <td className="spark-cell">
        <SparkCell data={spark} symbol={symbol} />
      </td>
      <td>{quote === undefined ? "" : (SESSION_LABEL[quote.mh] ?? "?")}</td>
      <td>
        {/* Not "no data": a symbol outside `yfin stream scope` has no
            live path at all, and that is a configuration answer rather
            than a missing one. */}
        {quote === undefined ? (
          <span className="muted">{close !== undefined ? "archived close" : spark.loading ? "loading" : spark.failed ? "archive unavailable" : "not streamed"}</span>
        ) : (
          <span className="muted">{new Date(quote.t).toISOString().slice(11, 19)}</span>
        )}
      </td>
      <td><button type="button" aria-label={`Remove ${symbol}`} onClick={onRemove}>Remove</button></td>
    </tr>
  );
}

export function WLA({ symbol, args }: PanelProps) {
  const go = usePanelRun();
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const symbols = parseSymbols(args.symbols);
  // `WLA` with a symbol on the strip and no list of its own watches that
  // one: the shortest way in, and it keeps the command meaningful from
  // the function bar where there are no arguments to type.
  const watched = symbols.length > 0 ? symbols : symbol === null ? [] : [symbol];
  const enabled = useLiveEnabled();
  const link = useLinkState();
  // One request for the whole list, not one per row: that is the whole
  // reason `/ui/api/sparklines` exists.
  const spark = useSparklines(watched);
  const open = (index: number) => {
    const picked = watched[index];
    if (picked !== undefined) {
      go({ symbol: picked, code: "DES", args: {} });
    }
  };
  const [selected] = useListKeys(watched.length, open);
  const update = (next: string[]) => {
    go({ symbol: null, code: "WLA", args: next.length ? { symbols: next.join(",") } : {} });
  };
  const controls = (
    <form className="watchlist-controls" onSubmit={(event) => {
      event.preventDefault();
      const tokens = draft.trim().split(/[\s,]+/).filter(Boolean);
      if (!tokens.length) { setError("Enter at least one symbol."); return; }
      const invalid = tokens.find((token) => !SYMBOL_RE.test(token.toUpperCase()));
      if (invalid) { setError(`${invalid} is not a valid symbol.`); return; }
      const next = [...new Set([...watched, ...tokens.map((token) => token.toUpperCase())])];
      if (next.length > WLA_MAX) { setError(`Watch up to ${WLA_MAX} symbols.`); return; }
      update(next);
      setDraft("");
      setError(null);
    }}>
      <label>Symbols <input aria-label="Watchlist symbols" placeholder="AAPL, MSFT, NVDA" value={draft} onChange={(event) => setDraft(event.target.value)} /></label>
      <button type="submit">Add symbols</button>
      {error && <p role="alert" className="warn">{error}</p>}
    </form>
  );

  if (watched.length === 0) {
    return (
      <section>
        {controls}
        <p className="muted">
          Nothing to watch yet. Type <code className="usage">{WLA_ARGS}</code> — the list is the
          URL, so it is shareable and Esc walks back through earlier ones.
        </p>
      </section>
    );
  }

  return (
    <section>
      {controls}
      <p className="chart-note">
        <span>
          {watched.length} {watched.length === 1 ? "symbol" : "symbols"} · UTC
        </span>
        {!enabled && <span className="muted">live stream off; these are last snapshots</span>}
        {enabled && link !== LinkState.Open && <span className="strip-link">reconnecting</span>}
      </p>
      <div className="table-scroll">
        <table className="grid" aria-label="watchlist">
          <thead>
            <tr>
              <th>Symbol</th>
              <th className="num">Price</th>
              <th className="num">Change</th>
              <th className="num">%</th>
              <th className="num">Volume</th>
              <th>{sparkLabel(spark)}</th>
              <th>Session</th>
              <th>Last</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {watched.map((code, index) => (
              <Row key={code} symbol={code} selected={index === selected} spark={spark} onOpen={() => open(index)} onRemove={() => update(watched.filter((item) => item !== code))} />
            ))}
          </tbody>
        </table>
      </div>
      <p className="chart-legend">
        <span className="muted">
          j / k moves, Enter or a symbol opens its details. Lists are saved in the URL;
          bookmark it to return. Archived closes are shown when no streamed quote is available.
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
