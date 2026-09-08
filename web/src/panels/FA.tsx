import { useMemo } from "react";
import { getFinancials, type FinancialFact } from "../api/client";
import { usePanelRun } from "../workspace/frame";
import { Layout, type PanelArgs, type PanelProps, type PanelSpec } from "../commands/types";
import {
  DataTable,
  EmptyCard,
  ErrorCard,
  LoadState,
  MissingCard,
  usePanelData,
  useSortedRows,
  type Column,
} from "./common";
import { asNumber, formatBig } from "./format";
import { Bars } from "./viz";

/** The API's statement kind. "valuation" exists on the API too but is not
 *  a statement a terminal user reads as one; it stays reachable through
 *  /v1 and out of the FA tabs. */
export enum Statement {
  Income = "income",
  Balance = "balance_sheet",
  Cash = "cash_flow",
}

export enum Freq {
  Annual = "annual",
  Quarterly = "quarterly",
  Ttm = "ttm",
}

//: One table per discriminator: the wire value, the word typed on the
//: command line, and the tab's label. Adding a statement is one line.
const STATEMENTS: Array<[value: Statement, word: string, label: string]> = [
  [Statement.Income, "income", "Income"],
  [Statement.Balance, "balance", "Balance sheet"],
  [Statement.Cash, "cash", "Cash flow"],
];
const FREQS: Array<[value: Freq, label: string]> = [
  [Freq.Annual, "Annual"],
  [Freq.Quarterly, "Quarterly"],
  [Freq.Ttm, "TTM"],
];

const STATEMENT_BY_WORD = new Map<string, Statement>(STATEMENTS.map(([value, word]) => [word, value]));
const STATEMENT_BY_VALUE = new Map<string, Statement>(STATEMENTS.map(([value]) => [value, value]));
const STATEMENT_LABEL = new Map<Statement, string>(STATEMENTS.map(([value, , label]) => [value, label]));
const FREQ_BY_VALUE = new Map<string, Freq>(FREQS.map(([value]) => [value, value]));
const FREQ_LABEL = new Map<Freq, string>(FREQS);

const DEFAULT_STATEMENT = Statement.Income;
const DEFAULT_FREQ = Freq.Annual;

export const FA_ARGS = `FA [${STATEMENTS.map(([, word]) => word).join("|")}] [${FREQS.map(([value]) => value).join("|")}]`;
export const FA_USAGE = `Usage: ${FA_ARGS}`;

function statementOf(value: string | undefined): Statement {
  return STATEMENT_BY_VALUE.get(value ?? "") ?? DEFAULT_STATEMENT;
}

function freqOf(value: string | undefined): Freq {
  return FREQ_BY_VALUE.get(value ?? "") ?? DEFAULT_FREQ;
}

//: At most this many periods are shown; older ones are still fetched but
//: would only widen the table past what fits on one screen.
const MAX_PERIODS = 8;

function parseArgs(tokens: string[]): PanelArgs {
  const statement = STATEMENT_BY_WORD.get((tokens[0] ?? "income").toLowerCase());
  const freq = FREQ_BY_VALUE.get((tokens[1] ?? "annual").toLowerCase());
  if (statement === undefined || freq === undefined) throw new Error(FA_USAGE);
  return { statement, freq };
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


//: The two lines every income statement has and every reader looks at
//: first. Yahoo's own item keys, which is what the archive stores.
export const REVENUE_ITEM = "TotalRevenue";
export const INCOME_ITEM = "NetIncome";

export interface IncomeChart {
  categories: string[];
  revenue: Array<number | null>;
  income: Array<number | null>;
  /** Net income as a percentage of revenue, on its own axis: a margin of
   *  25 next to a revenue of 400 billion shares no scale with it. */
  margin: Array<number | null>;
}

/** Revenue and net income per period, oldest first, or null when the
 *  statement does not carry both.
 *
 *  Oldest first, unlike the table beside it: a table is read down from
 *  the newest row, and a time axis is read left to right. Null when
 *  either item is absent -- a balance sheet has no revenue, and half a
 *  chart is worse than none.
 */
export function incomeChart(table: Pivot, freq: Freq): IncomeChart | null {
  const revenueRow = table.rows.find((row) => row.item === REVENUE_ITEM);
  const incomeRow = table.rows.find((row) => row.item === INCOME_ITEM);
  if (revenueRow === undefined || incomeRow === undefined) return null;
  const periods = [...table.periods].reverse();
  const revenue = periods.map((period) => asNumber(revenueRow[period]));
  const income = periods.map((period) => asNumber(incomeRow[period]));
  if (revenue.every((value) => value === null)) return null;
  return {
    // A fiscal year is its year; a quarter needs the month to be told
    // from the next one. A full ISO date would not fit on the axis.
    categories: periods.map((period) =>
      freq === Freq.Annual ? period.slice(0, 4) : period.slice(0, 7),
    ),
    revenue,
    income,
    margin: revenue.map((value, index) => {
      const net = income[index];
      // A margin off a zero or negative revenue is not a percentage of
      // anything a reader can act on.
      return value === null || value <= 0 || net === null || net === undefined
        ? null
        : (net / value) * 100;
    }),
  };
}

export function FA({ symbol, args }: PanelProps) {
  const go = usePanelRun();
  const statement = statementOf(args.statement);
  const freq = freqOf(args.freq);
  const { state, retry } = usePanelData<FinancialFact[]>(
    `${symbol ?? ""}|${statement}|${freq}`,
    () => (symbol === null ? Promise.reject(new Error("no symbol")) : getFinancials(symbol, statement, freq)),
    (rows) => rows.length === 0,
  );
  const table = useMemo(() => (state.kind === LoadState.Ready ? pivot(state.data) : null), [state]);
  // Only the income statement has a revenue and a net income; the
  // balance sheet and the cash flow get their table and nothing else.
  const chart = useMemo(
    () => (table !== null && statement === Statement.Income ? incomeChart(table, freq) : null),
    [table, statement, freq],
  );

  if (symbol === null) return null;

  function switchTo(next: PanelArgs) {
    go({ symbol, code: "FA", args: { statement, freq, ...next } });
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
        {STATEMENTS.map(([value, , label]) => (
          <button
            key={value}
            role="tab"
            aria-selected={value === statement}
            className={value === statement ? "tab tab-active" : "tab"}
            onClick={() => switchTo({ statement: value })}
          >
            {label}
          </button>
        ))}
      </div>
      <div className="tabs" role="tablist" aria-label="frequency">
        {FREQS.map(([value, label]) => (
          <button
            key={value}
            role="tab"
            aria-selected={value === freq}
            className={value === freq ? "tab tab-active" : "tab"}
            onClick={() => switchTo({ freq: value })}
          >
            {label}
          </button>
        ))}
      </div>
      {state.kind === LoadState.Loading && <p className="muted">Loading {symbol}…</p>}
      {state.kind === LoadState.Missing && <MissingCard symbol={symbol} />}
      {state.kind === LoadState.Error && <ErrorCard message={state.message} onRetry={retry} />}
      {state.kind === LoadState.Empty && <EmptyCard what="financial statements" />}
      {table && (
        <>
          <p className="muted">
            {symbol} · {STATEMENT_LABEL.get(statement)} · {FREQ_LABEL.get(freq)}
            {table.currency ? ` · ${table.currency}` : ""}
          </p>
          {/* Above the table, never instead of it: the one thing a chart
              cannot show is the exact figure (spec, "Kararlar" 3). */}
          {chart !== null && (
            <Bars
              label={`${symbol} revenue and net income, ${FREQ_LABEL.get(freq)?.toLowerCase()}`}
              categories={chart.categories}
              series={[
                { key: "revenue", label: "Revenue", values: chart.revenue },
                { key: "income", label: "Net income", values: chart.income },
              ]}
              line={{ label: "Net margin %", values: chart.margin }}
              format={cell}
            />
          )}
          <StatementTable columns={columns} rows={table.rows} />
        </>
      )}
    </section>
  );
}

/** The statement, in whatever order the reader clicked a period into.
 *  A hook cannot live inside the conditional above, so the table is its
 *  own component. */
function StatementTable({ columns, rows }: { columns: Column<PivotRow>[]; rows: PivotRow[] }) {
  const { rows: ordered, sort, toggle } = useSortedRows(rows);
  return (
    <DataTable
      columns={columns}
      rows={ordered}
      rowKey={(row) => row.item}
      sort={sort}
      onSort={toggle}
    />
  );
}

export const FA_PANEL: PanelSpec = {
  code: "FA",
  title: "Financial statements",
  usage: FA_ARGS,
  needsSymbol: true,
  layout: Layout.Single,
  parseArgs,
  // Args can arrive from a hand-edited URL, not only from parseArgs.
  normalizeArgs: (args) => ({
    ...args,
    statement: statementOf(args.statement),
    freq: freqOf(args.freq),
  }),
  component: FA,
};
