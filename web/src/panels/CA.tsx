// CA: corporate actions (dividends, splits, capital gains), newest first.
import { useMemo } from "react";
import { getActions, WireType, type CatalogColumn, type Row, type Rows } from "../api/client";
import { Layout, type PanelProps, type PanelSpec } from "../commands/types";
import { trimDecimal } from "./chart-data";
import { EmptyCard, ErrorCard, LoadState, MissingCard, usePanelData } from "./common";
import { asNumber } from "./format";
import { DatasetTable } from "./table";
import { Bars, type BarMark } from "./viz";

//: The Action schema, as columns for the typed table.
export const ACTION_COLUMNS: CatalogColumn[] = [
  { name: "symbol", type: WireType.String, nullable: false },
  { name: "action_date", type: WireType.Date, nullable: false },
  { name: "action_type", type: WireType.String, nullable: false },
  { name: "action_value", type: WireType.Decimal, nullable: false },
];

//: The symbol is on the band above every row of this table; the grid
//: says what the action was.
const CA_GRID = ["action_date", "action_type", "action_value"];


//: The action types the archive writes. Only two of them belong on this
//: chart: a dividend has an amount to stack up, a split has a date and a
//: ratio, and a capital gain is neither often enough to draw.
const DIVIDEND = "DIVIDEND";
const SPLIT = "SPLIT";

//: Years on the axis. Twenty is already a wide chart; the table below
//: still holds every action the archive has.
export const MAX_YEARS = 20;

export interface DividendChart {
  years: string[];
  /** Total paid per calendar year, in the symbol's currency. */
  amounts: Array<number | null>;
  /** Splits, on the year they happened. */
  marks: BarMark[];
}

/** Dividends summed per calendar year, oldest first, with splits marked.
 *  Per YEAR: the question is whether the payout is growing. A split has
 *  no amount, so it goes on the axis rather than claiming a bar. */
export function dividendChart(rows: Row[], maxYears: number = MAX_YEARS): DividendChart | null {
  const byYear = new Map<string, number>();
  const splits = new Map<string, string>();
  for (const row of rows) {
    const year = String(row.action_date ?? "").slice(0, 4);
    if (year.length !== 4) continue;
    const value = asNumber(row.action_value);
    if (row.action_type === DIVIDEND && value !== null) {
      byYear.set(year, (byYear.get(year) ?? 0) + value);
    } else if (row.action_type === SPLIT && value !== null) {
      splits.set(year, `${trimDecimal(row.action_value)}-for-1 split`);
    }
  }
  if (byYear.size === 0) return null;
  const years = [...byYear.keys()].sort().slice(-maxYears);
  const shown = new Set(years);
  return {
    years,
    amounts: years.map((year) => byYear.get(year) ?? null),
    marks: [...splits]
      .filter(([year]) => shown.has(year))
      .map(([year, label]) => ({ category: year, label })),
  };
}

export function CA({ symbol }: PanelProps) {
  const { state, retry } = usePanelData<Rows>(
    symbol ?? "",
    () => (symbol === null ? Promise.reject(new Error("no symbol")) : getActions(symbol)),
    (data) => data.rows.length === 0,
  );
  const chart = useMemo(
    () => (state.kind === LoadState.Ready ? dividendChart(state.data.rows) : null),
    [state],
  );
  if (symbol === null) return null;
  if (state.kind === LoadState.Loading) return <p className="muted">Loading {symbol}…</p>;
  if (state.kind === LoadState.Missing) return <MissingCard symbol={symbol} />;
  if (state.kind === LoadState.Error) return <ErrorCard message={state.message} onRetry={retry} />;
  if (state.kind === LoadState.Empty) return <EmptyCard what="corporate actions" />;
  return (
    <section>
      <p className="detail-meta">Dividends, splits and capital gains, newest first.</p>
      {chart !== null && (
        <Bars
          label={`${symbol} dividends per year, with splits marked`}
          categories={chart.years}
          series={[{ key: "dividend", label: "Paid per year", values: chart.amounts }]}
          marks={chart.marks}
          format={(value) => value.toFixed(2)}
        />
      )}
      <DatasetTable columns={ACTION_COLUMNS} rows={state.data.rows} grid={CA_GRID} reverse truncated={state.data.truncated} />
    </section>
  );
}

export const CA_PANEL: PanelSpec = {
  code: "CA",
  title: "Corporate actions: dividends, splits, capital gains",
  needsSymbol: true,
  layout: Layout.Single,
  parseArgs: () => ({}),
  component: CA,
};
