// PX: price bars as a table, newest first. Charts (GP/GIP) are 1d's; this
// is the archive's bars, every column, readable now.
import { BAR_INTERVALS, PAGE_LIMIT, getBars, type CatalogColumn, type Row } from "../api/client";
import type { PanelArgs, PanelProps, PanelSpec } from "../commands/types";
import { EmptyCard, ErrorCard, MissingCard, usePanelData } from "./common";
import { DatasetTable } from "./table";

export const PX_USAGE = `Usage: PX [${BAR_INTERVALS.join("|")}] [rows 1-${PAGE_LIMIT}]`;
const DEFAULT_ROWS = 250;

function parseArgs(tokens: string[]): PanelArgs {
  const interval = tokens[0] ?? "1d";
  const rows = tokens[1] === undefined ? DEFAULT_ROWS : Number(tokens[1]);
  if (!BAR_INTERVALS.includes(interval)) throw new Error(PX_USAGE);
  if (!Number.isInteger(rows) || rows < 1 || rows > PAGE_LIMIT) throw new Error(PX_USAGE);
  return { interval, rows: String(rows) };
}

//: The Bar schema, as columns for the typed table.
export const BAR_COLUMNS: CatalogColumn[] = [
  { name: "symbol", type: "string", nullable: false },
  { name: "ts_utc", type: "string (date-time)", nullable: false },
  { name: "bar_interval", type: "string", nullable: true },
  { name: "session_date", type: "string (date)", nullable: true },
  { name: "local_date", type: "string (date)", nullable: true },
  { name: "open", type: "string (decimal)", nullable: true },
  { name: "high", type: "string (decimal)", nullable: true },
  { name: "low", type: "string (decimal)", nullable: true },
  { name: "close", type: "string (decimal)", nullable: true },
  { name: "adj_close", type: "string (decimal)", nullable: true },
  { name: "volume", type: "integer", nullable: true },
  { name: "is_extended", type: "boolean", nullable: true },
];

export function PX({ symbol, args }: PanelProps) {
  const interval = args.interval ?? "1d";
  const rows = Number(args.rows ?? DEFAULT_ROWS);
  const { state, retry } = usePanelData<Row[]>(
    `${symbol ?? ""}|${interval}|${rows}`,
    () => (symbol === null ? Promise.reject(new Error("no symbol")) : getBars(symbol, interval, rows)),
    (data) => data.length === 0,
  );
  if (symbol === null) return null;
  if (state.kind === "loading") return <p className="muted">Loading {symbol} {interval} bars…</p>;
  if (state.kind === "missing") return <MissingCard symbol={symbol} />;
  if (state.kind === "error") return <ErrorCard message={state.message} onRetry={retry} />;
  if (state.kind === "empty") return <EmptyCard what={`${interval} bars`} />;
  return (
    <section>
      <p className="detail-meta">
        The newest {rows} {interval} bars, newest first. {PX_USAGE.slice(7)}
      </p>
      <DatasetTable columns={BAR_COLUMNS} rows={state.data} hide={["symbol"]} reverse />
    </section>
  );
}

export const PX_PANEL: PanelSpec = {
  code: "PX",
  title: "Price bars as a table",
  usage: PX_USAGE.slice(7),
  needsSymbol: true,
  layout: "single",
  parseArgs,
  component: PX,
};
