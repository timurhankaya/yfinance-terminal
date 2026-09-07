// CA: corporate actions (dividends, splits, capital gains), newest first.
import { getActions, type CatalogColumn, type Rows } from "../api/client";
import type { PanelProps, PanelSpec } from "../commands/types";
import { EmptyCard, ErrorCard, MissingCard, usePanelData } from "./common";
import { DatasetTable } from "./table";

//: The Action schema, as columns for the typed table.
export const ACTION_COLUMNS: CatalogColumn[] = [
  { name: "symbol", type: "string", nullable: false },
  { name: "action_date", type: "string (date)", nullable: false },
  { name: "action_type", type: "string", nullable: false },
  { name: "action_value", type: "string (decimal)", nullable: false },
];

export function CA({ symbol }: PanelProps) {
  const { state, retry } = usePanelData<Rows>(
    symbol ?? "",
    () => (symbol === null ? Promise.reject(new Error("no symbol")) : getActions(symbol)),
    (data) => data.rows.length === 0,
  );
  if (symbol === null) return null;
  if (state.kind === "loading") return <p className="muted">Loading {symbol}…</p>;
  if (state.kind === "missing") return <MissingCard symbol={symbol} />;
  if (state.kind === "error") return <ErrorCard message={state.message} onRetry={retry} />;
  if (state.kind === "empty") return <EmptyCard what="corporate actions" />;
  return (
    <section>
      <p className="detail-meta">Dividends, splits and capital gains, newest first.</p>
      <DatasetTable columns={ACTION_COLUMNS} rows={state.data.rows} hide={["symbol"]} reverse truncated={state.data.truncated} />
    </section>
  );
}

export const CA_PANEL: PanelSpec = {
  code: "CA",
  title: "Corporate actions: dividends, splits, capital gains",
  needsSymbol: true,
  layout: "single",
  parseArgs: () => ({}),
  component: CA,
};
