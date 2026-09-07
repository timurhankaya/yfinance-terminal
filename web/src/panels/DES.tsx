import { Fragment, useCallback, useEffect, useState } from "react";
import { ApiError, UnauthorizedError, getSymbol, type SymbolDetail } from "../api/client";
import { useSession } from "../app/session";

type State =
  | { kind: "loading" }
  | { kind: "ready"; detail: SymbolDetail }
  | { kind: "missing" }
  | { kind: "error" };

type Kind = "text" | "big" | "num" | "pct";

const INFO_ROWS: ReadonlyArray<[key: string, label: string, kind: Kind]> = [
  ["sector", "Sector", "text"],
  ["industry", "Industry", "text"],
  ["marketCap", "Market cap", "big"],
  ["trailingPE", "P/E (ttm)", "num"],
  ["forwardPE", "P/E (fwd)", "num"],
  ["dividendYield", "Dividend yield", "pct"],
  ["beta", "Beta", "num"],
  ["fiftyTwoWeekLow", "52w low", "num"],
  ["fiftyTwoWeekHigh", "52w high", "num"],
  ["website", "Website", "text"],
];

export function formatBig(value: number): string {
  const units: Array<[number, string]> = [[1e12, "T"], [1e9, "B"], [1e6, "M"], [1e3, "K"]];
  for (const [size, suffix] of units) {
    if (Math.abs(value) >= size) return `${(value / size).toFixed(2)}${suffix}`;
  }
  return value.toFixed(0);
}

function format(value: unknown, kind: Kind): string | null {
  if (value === null || value === undefined) return null;
  if (kind === "text") return String(value);
  if (typeof value !== "number" || Number.isNaN(value)) return null;
  if (kind === "big") return formatBig(value);
  if (kind === "pct") return `${(value * 100).toFixed(2)}%`;
  return value.toFixed(2);
}

export function DES({ symbol }: { symbol: string }) {
  const { me, requireLogin } = useSession();
  const authenticated = me?.authenticated === true;
  const [state, setState] = useState<State>({ kind: "loading" });

  // `cancelled` guards against a stale response overwriting fresher data:
  // if `symbol` changes while a fetch is in flight, the effect below marks
  // the old call cancelled and its `setState`s after the await are skipped.
  const load = useCallback(async (cancelled: () => boolean) => {
    setState({ kind: "loading" });
    try {
      const detail = await getSymbol(symbol);
      if (cancelled()) return;
      setState({ kind: "ready", detail });
    } catch (err) {
      if (cancelled()) return;
      if (err instanceof UnauthorizedError) requireLogin();
      else if (err instanceof ApiError && err.status === 404) setState({ kind: "missing" });
      else setState({ kind: "error" });
    }
  }, [symbol, requireLogin]);

  // Runs when the symbol changes AND when the session comes back after a
  // login: the spec's "the last command re-runs after a successful login".
  useEffect(() => {
    if (!authenticated) return;
    let cancelled = false;
    void load(() => cancelled);
    return () => {
      cancelled = true;
    };
  }, [load, authenticated]);

  if (state.kind === "loading") return <p className="muted">Loading {symbol}…</p>;
  if (state.kind === "missing") return <p className="error">No such symbol: {symbol}</p>;
  if (state.kind === "error")
    return (
      <p className="error">
        Could not load {symbol}. <button onClick={() => void load(() => false)}>Retry</button>
      </p>
    );

  const d = state.detail;
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
          return (
            <Fragment key={key}>
              <dt>{label}</dt><dd>{text}</dd>
            </Fragment>
          );
        })}
      </dl>
      {d.info === null && <p className="muted">Never synced: run yfin sync --symbols {d.symbol}</p>}
    </section>
  );
}
