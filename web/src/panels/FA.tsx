import { useMemo } from "react";
import { useNavigate } from "react-router";
import { getFinancials, type FinancialFact } from "../api/client";
import { commandToPath } from "../commands/parser";
import type { PanelArgs, PanelProps, PanelSpec } from "../commands/types";
import { asNumber, formatBig } from "./DES";
import { DataTable, EmptyCard, ErrorCard, MissingCard, usePanelData, type Column } from "./common";

// The command word -> the API's statement kind. "valuation" exists on the
// API too but is not a statement a terminal user reads as one; it stays
// reachable through /v1 and out of the FA tabs.
const STATEMENTS: Record<string, string> = {
  income: "income",
  balance: "balance_sheet",
  cash: "cash_flow",
};
const FREQS = ["annual", "quarterly", "ttm"];
const USAGE = "Usage: FA [income|balance|cash] [annual|quarterly|ttm]";

const STATEMENT_TABS: Array<[value: string, label: string]> = [
  ["income", "Income"],
  ["balance_sheet", "Balance sheet"],
  ["cash_flow", "Cash flow"],
];
const FREQ_TABS: Array<[value: string, label: string]> = [
  ["annual", "Annual"],
  ["quarterly", "Quarterly"],
  ["ttm", "TTM"],
];

//: At most this many periods are shown; older ones are still fetched but
//: would only widen the table past what fits on one screen.
const MAX_PERIODS = 8;

function parseArgs(tokens: string[]): PanelArgs {
  const s = (tokens[0] ?? "income").toLowerCase();
  const f = (tokens[1] ?? "annual").toLowerCase();
  const statement = STATEMENTS[s];
  if (statement === undefined || !FREQS.includes(f)) throw new Error(USAGE);
  return { statement, freq: f };
}

interface PivotRow extends Record<string, unknown> {
  item: string;
}

interface Pivot {
  periods: string[];
  rows: PivotRow[];
  currency: string | null;
}

/** One row per item_key (first-seen order), one column per period_end (newest first). */
export function pivot(facts: FinancialFact[]): Pivot {
  const periods = [...new Set(facts.map((f) => f.period_end))].sort().reverse().slice(0, MAX_PERIODS);
  const shown = new Set(periods);
  const items: string[] = [];
  const cells = new Map<string, string>();
  for (const fact of facts) {
    if (!items.includes(fact.item_key)) items.push(fact.item_key);
    if (shown.has(fact.period_end)) cells.set(`${fact.item_key}|${fact.period_end}`, fact.value);
  }
  const rows: PivotRow[] = items.map((item) => {
    const row: PivotRow = { item };
    for (const period of periods) row[period] = cells.get(`${item}|${period}`) ?? null;
    return row;
  });
  return { periods, rows, currency: facts[0]?.currency ?? null };
}

// Statements mix magnitudes: revenue in the hundreds of billions next to
// EPS at 8.76 and tax rates at 0.17. Below a thousand the K/M/B/T scaler
// would round those to "9" and "0", so small values keep two decimals.
export function cell(value: unknown): string {
  const n = asNumber(value);
  if (n === null) return "—";
  return Math.abs(n) < 1000 ? n.toFixed(2) : formatBig(n);
}

export function FA({ symbol, args }: PanelProps) {
  const navigate = useNavigate();
  const statement = args.statement ?? "income";
  const freq = args.freq ?? "annual";
  const { state, retry } = usePanelData<FinancialFact[]>(
    `${symbol ?? ""}|${statement}|${freq}`,
    () => (symbol === null ? Promise.reject(new Error("no symbol")) : getFinancials(symbol, statement, freq)),
    (rows) => rows.length === 0,
  );
  const table = useMemo(() => (state.kind === "ready" ? pivot(state.data) : null), [state]);

  if (symbol === null) return null;

  function go(next: PanelArgs) {
    void navigate(commandToPath({ symbol, code: "FA", args: { statement, freq, ...next } }));
  }

  const columns: Column<PivotRow>[] = table
    ? [
        { key: "item", label: "Item" },
        ...table.periods.map((period) => ({
          key: period,
          label: period,
          align: "right" as const,
          format: (row: PivotRow) => cell(row[period]),
        })),
      ]
    : [];

  return (
    <section>
      <div className="tabs" role="tablist" aria-label="statement">
        {STATEMENT_TABS.map(([value, label]) => (
          <button
            key={value}
            role="tab"
            aria-selected={value === statement}
            className={value === statement ? "tab tab-active" : "tab"}
            onClick={() => go({ statement: value })}
          >
            {label}
          </button>
        ))}
      </div>
      <div className="tabs" role="tablist" aria-label="frequency">
        {FREQ_TABS.map(([value, label]) => (
          <button
            key={value}
            role="tab"
            aria-selected={value === freq}
            className={value === freq ? "tab tab-active" : "tab"}
            onClick={() => go({ freq: value })}
          >
            {label}
          </button>
        ))}
      </div>
      {state.kind === "loading" && <p className="muted">Loading {symbol}…</p>}
      {state.kind === "missing" && <MissingCard symbol={symbol} />}
      {state.kind === "error" && <ErrorCard message={state.message} onRetry={retry} />}
      {state.kind === "empty" && <EmptyCard what="financial statements" />}
      {table && (
        <>
          <p className="muted">
            {symbol} · {STATEMENT_TABS.find(([v]) => v === statement)?.[1] ?? statement} ·{" "}
            {FREQ_TABS.find(([v]) => v === freq)?.[1] ?? freq}
            {table.currency ? ` · ${table.currency}` : ""}
          </p>
          <DataTable columns={columns} rows={table.rows} rowKey={(row) => row.item} />
        </>
      )}
    </section>
  );
}

export const FA_PANEL: PanelSpec = {
  code: "FA",
  title: "Financial statements",
  needsSymbol: true,
  layout: "single",
  parseArgs,
  component: FA,
};
