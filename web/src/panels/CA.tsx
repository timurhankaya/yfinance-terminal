// CA: corporate actions (dividends, splits, capital gains), newest first.
import { getActions, WireType, type CatalogColumn, type Rows } from "../api/client";
import { Layout, type PanelProps, type PanelSpec } from "../commands/types";
import { EmptyCard, ErrorCard, LoadState, MissingCard, usePanelData } from "./common";
import { DatasetTable } from "./table";

//: The Action schema, as columns for the typed table.
export const ACTION_COLUMNS: CatalogColumn[] = [
  { name: "symbol", type: WireType.String, nullable: false },
  { name: "action_date", type: WireType.Date, nullable: false },
  { name: "action_type", type: WireType.String, nullable: false },
  { name: "action_value", type: WireType.Decimal, nullable: false },
];

export function CA({ symbol }: PanelProps) {
  const { state, retry } = usePanelData<Rows>(
    symbol ?? "",
    () => (symbol === null ? Promise.reject(new Error("no symbol")) : getActions(symbol)),
    (data) => data.rows.length === 0,
  );
  if (symbol === null) return null;
  if (state.kind === LoadState.Loading) return <p className="muted">Loading {symbol}…</p>;
  if (state.kind === LoadState.Missing) return <MissingCard symbol={symbol} />;
  if (state.kind === LoadState.Error) return <ErrorCard message={state.message} onRetry={retry} />;
  if (state.kind === LoadState.Empty) return <EmptyCard what="corporate actions" />;
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
  layout: Layout.Single,
  parseArgs: () => ({}),
  component: CA,
};
