import { Fragment } from "react";
import { getSymbol, type SymbolDetail } from "../api/client";
import type { PanelProps, PanelSpec } from "../commands/types";
import { ErrorCard, MissingCard, usePanelData } from "./common";

type Kind = "text" | "big" | "num" | "pct" | "link";

// Keys are the API's: the `info` snapshot is normalised to snake_case on
// the way into the database, not yfinance's camelCase. Numbers arrive as
// strings (Decimal on the wire), and `dividend_yield` is already a
// percentage (0.34 means 0.34 %), so `pct` appends the sign without scaling.
const INFO_ROWS: ReadonlyArray<[key: string, label: string, kind: Kind]> = [
  ["sector", "Sector", "text"],
  ["industry", "Industry", "text"],
  ["market_cap", "Market cap", "big"],
  ["trailing_pe", "P/E (ttm)", "num"],
  ["forward_pe", "P/E (fwd)", "num"],
  ["dividend_yield", "Dividend yield", "pct"],
  ["beta", "Beta", "num"],
  ["fifty_two_week_low", "52w low", "num"],
  ["fifty_two_week_high", "52w high", "num"],
  ["website", "Website", "link"],
];

function isHttpUrl(value: unknown): value is string {
  return typeof value === "string" && /^https?:\/\//i.test(value);
}

export function formatBig(value: number): string {
  const units: Array<[number, string]> = [[1e12, "T"], [1e9, "B"], [1e6, "M"], [1e3, "K"]];
  for (const [size, suffix] of units) {
    if (Math.abs(value) >= size) return `${(value / size).toFixed(2)}${suffix}`;
  }
  return value.toFixed(0);
}

export function asNumber(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function format(value: unknown, kind: Kind): string | null {
  if (value === null || value === undefined) return null;
  if (kind === "text" || kind === "link") return String(value);
  const n = asNumber(value);
  if (n === null) return null;
  if (kind === "big") return formatBig(n);
  if (kind === "pct") return `${n.toFixed(2)}%`;
  return n.toFixed(2);
}

export function DES({ symbol }: PanelProps) {
  // symbol can be null in the general PanelProps shape (a panel row can be
  // rendered before a symbol is chosen); guard the load itself rather than
  // skipping the hook call, which React's rules of hooks forbid.
  const { state, retry } = usePanelData<SymbolDetail>(symbol ?? "", () =>
    symbol === null ? Promise.reject(new Error("no symbol")) : getSymbol(symbol),
  );

  if (symbol === null) return null;
  if (state.kind === "loading") return <p className="muted">Loading {symbol}…</p>;
  if (state.kind === "missing") return <MissingCard symbol={symbol} />;
  if (state.kind === "error") return <ErrorCard message={state.message} onRetry={retry} />;
  if (state.kind === "empty") return null;

  const d = state.data;
  const info = d.info ?? {};
  return (
    <section>
      <h2>{d.long_name ?? d.short_name ?? d.symbol}</h2>
      <dl className="des">
        <dt>Symbol</dt><dd>{d.symbol}</dd>
        <dt>Exchange</dt><dd>{d.full_exchange_name ?? d.exchange ?? "—"}</dd>
        <dt>Type</dt><dd>{d.quote_type ?? "—"}</dd>
        <dt>Currency</dt><dd>{d.currency ?? "—"}</dd>
        <dt>Timezone</dt><dd>{d.timezone ?? "—"}</dd>
        {INFO_ROWS.map(([key, label, kind]) => {
          const text = format(info[key], kind);
          if (text === null) return null;
          // Fragment, not a wrapper: <dl> only allows dt/dd children, and
          // an inline style= would be blocked by the page's CSP anyway.
          const value = info[key];
          return (
            <Fragment key={key}>
              <dt>{label}</dt>
              <dd>
                {kind === "link" && isHttpUrl(value) ? (
                  <a href={value} target="_blank" rel="noopener noreferrer">{text}</a>
                ) : (
                  text
                )}
              </dd>
            </Fragment>
          );
        })}
      </dl>
      {d.info === null && <p className="muted">Never synced: run yfin sync --symbols {d.symbol}</p>}
    </section>
  );
}

export const DES_PANEL: PanelSpec = {
  code: "DES",
  title: "Description",
  needsSymbol: true,
  layout: "headed",
  parseArgs: () => ({}),
  component: DES,
};
